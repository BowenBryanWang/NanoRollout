"""EC2-backed sandbox runtime for UDA tasks.

Launches a VM that exposes the same uda-desktop HTTP surface as docker
and modal on ``:8080``. The agent loop remains unchanged: once the
instance is healthy, ``UnifiedSandboxClient`` talks to
``http://<instance-ip>:8080`` for shell/file/code/jupyter/computer-use.

This runtime deliberately does not use SSH/SCP. Host-to-runtime copy and
in-runtime exec inherit :class:`BaseSandboxRuntime`'s SDK-mediated HTTP
implementation, matching modal's data path.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from .base import BaseSandboxRuntime, runtime_logger


def _csv_or_list(value: Any) -> List[str]:
    """Normalize a comma-separated string or list into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _bypass_proxy_env() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


class EC2SandboxRuntime(BaseSandboxRuntime):
    """Lifecycle manager backed by AWS EC2 instances."""

    runtime_type = "ec2"

    def __init__(self, client: Any):
        super().__init__(client)
        self.instance_id: Optional[str] = None
        self.owns_instance = False
        self.ec2 = None
        self.region = ""

    def start(self, task: Dict[str, Any], wait_time: int = 60) -> bool:
        """Launch or attach to an EC2 instance and wait for ``/v1/sandbox``."""
        try:
            cfg = self.client.sandbox_config
            if _truthy(cfg.get("ec2_bypass_proxy"), default=True):
                _bypass_proxy_env()

            import boto3

            task_name = task.get("task_name", "task")
            task_dir = task.get("task_dir")
            self.client.task_name = task_name
            self.client.task_dir = task_dir

            self.region = (
                cfg.get("ec2_region")
                or cfg.get("aws_region")
                or os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
                or "us-west-2"
            )
            aws_profile = cfg.get("aws_profile") or cfg.get("ec2_profile")
            session_kwargs: Dict[str, Any] = {"region_name": self.region}
            if aws_profile:
                session_kwargs["profile_name"] = aws_profile
            session = boto3.Session(**session_kwargs)
            self.ec2 = session.client("ec2")

            configured_instance_id = cfg.get("ec2_instance_id")
            if configured_instance_id:
                self.instance_id = configured_instance_id
                self.owns_instance = bool(cfg.get("ec2_terminate_attached_instance", False))
                runtime_logger.info("Attaching to EC2 instance %s", self.instance_id)
            else:
                self.instance_id = self._launch_instance(task)
                self.owns_instance = True

            if not self.instance_id:
                runtime_logger.error("EC2 runtime did not receive an instance id")
                return False

            instance = self._wait_for_instance_running(
                self.instance_id,
                int(cfg.get("ec2_instance_running_timeout", 300)),
            )
            ip = self._select_instance_ip(instance)
            if not ip:
                runtime_logger.error("EC2 instance %s has no reachable IP", self.instance_id)
                self.cleanup()
                return False

            port = int(cfg.get("ec2_container_port", cfg.get("ec2_port", 8080)))
            self.client.set_base_url(f"http://{ip}:{port}")
            self.client.runtime_id = self.instance_id
            self.client.container_id = self.instance_id
            self.client._update_runtime_metadata(
                instance_id=self.instance_id,
                region=self.region,
                workspace_dir=cfg.get("ec2_workspace_dir") or "/home/user",
                availability_zone=instance.get("Placement", {}).get("AvailabilityZone"),
                private_ip=instance.get("PrivateIpAddress"),
                public_ip=instance.get("PublicIpAddress"),
                selected_ip=ip,
                container_port=port,
                task_name=task_name,
                task_dir=task_dir,
                bench=self.client.sandbox_config.get("bench"),
                ec2_profile=self.client.sandbox_config.get("ec2_env_profile")
                or self.client.sandbox_config.get("ec2_profile_name")
                or self.client.sandbox_config.get("env_profile"),
                launch_template_id=self.client.sandbox_config.get("ec2_launch_template_id"),
                launch_template_name=self.client.sandbox_config.get("ec2_launch_template_name"),
                ami_id=instance.get("ImageId"),
                instance_type=instance.get("InstanceType"),
                subnet_id=instance.get("SubnetId"),
                security_group_ids=[
                    group.get("GroupId") for group in instance.get("SecurityGroups", [])
                ],
            )

            health_timeout = max(wait_time, int(cfg.get("ec2_health_timeout", wait_time)))
            cfg.setdefault("health_successes", int(cfg.get("ec2_health_successes", 2)))
            cfg.setdefault("post_health_delay", float(cfg.get("ec2_post_health_delay", 10)))
            if self._wait_for_health(health_timeout):
                runtime_logger.info("EC2 sandbox environment ready (%s)", self.instance_id)
                return True

            runtime_logger.error(
                "EC2 sandbox %s failed health check within %s seconds",
                self.instance_id,
                health_timeout,
            )
            self.cleanup()
            return False
        except Exception as exc:
            runtime_logger.error("Error creating EC2 sandbox: %s", exc)
            self.cleanup()
            return False

    def cleanup(self) -> bool:
        """Terminate the owned EC2 instance unless configured otherwise."""
        if not self.instance_id or self.ec2 is None:
            runtime_logger.info("No EC2 instance to clean up")
            return True

        if not self.owns_instance:
            terminate_attached = bool(
                self.client.sandbox_config.get("ec2_terminate_attached_instance", False)
            )
            if not terminate_attached:
                runtime_logger.info("Leaving attached EC2 instance running: %s", self.instance_id)
                self._clear_client_runtime()
                return True

        terminate = bool(self.client.sandbox_config.get("ec2_terminate_on_cleanup", True))
        if not terminate:
            runtime_logger.info("Leaving EC2 instance running by config: %s", self.instance_id)
            self._clear_client_runtime()
            return True

        try:
            runtime_logger.info("Terminating EC2 instance %s", self.instance_id)
            self.ec2.terminate_instances(InstanceIds=[self.instance_id])
            if bool(self.client.sandbox_config.get("ec2_wait_for_termination", False)):
                waiter = self.ec2.get_waiter("instance_terminated")
                waiter.wait(
                    InstanceIds=[self.instance_id],
                    WaiterConfig={"Delay": 10, "MaxAttempts": 60},
                )
            self._clear_client_runtime()
            return True
        except Exception as exc:
            err = getattr(exc, "response", {}).get("Error", {})
            if err.get("Code") in {"InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"}:
                runtime_logger.info("EC2 instance already absent during cleanup: %s", self.instance_id)
                self._clear_client_runtime()
                return True
            runtime_logger.error("Error terminating EC2 instance %s: %s", self.instance_id, exc)
            return False

    def _launch_instance(self, task: Dict[str, Any]) -> str:
        cfg = self.client.sandbox_config
        kwargs: Dict[str, Any] = {"MinCount": 1, "MaxCount": 1}

        launch_template_id = cfg.get("ec2_launch_template_id")
        launch_template_name = cfg.get("ec2_launch_template_name")
        launch_template_version = cfg.get("ec2_launch_template_version")
        if launch_template_id or launch_template_name:
            lt: Dict[str, str] = {}
            if launch_template_id:
                lt["LaunchTemplateId"] = str(launch_template_id)
            if launch_template_name:
                lt["LaunchTemplateName"] = str(launch_template_name)
            if launch_template_version:
                lt["Version"] = str(launch_template_version)
            kwargs["LaunchTemplate"] = lt
        else:
            ami_id = cfg.get("ec2_ami_id") or cfg.get("ami_id")
            if not ami_id:
                raise ValueError(
                    "EC2 runtime requires ec2_ami_id/ami_id or ec2_launch_template_id/name"
                )
            kwargs["ImageId"] = ami_id

        for cfg_key, run_key in (
            ("ec2_instance_type", "InstanceType"),
            ("ec2_key_name", "KeyName"),
        ):
            value = cfg.get(cfg_key)
            if value:
                kwargs[run_key] = value

        sg_ids = _csv_or_list(cfg.get("ec2_security_group_ids") or cfg.get("security_group_ids"))
        subnet_id = cfg.get("ec2_subnet_id")
        if subnet_id and not (launch_template_id or launch_template_name):
            nic: Dict[str, Any] = {
                "SubnetId": subnet_id,
                "DeviceIndex": 0,
                "AssociatePublicIpAddress": not bool(cfg.get("ec2_disable_public_ip", False)),
            }
            if sg_ids:
                nic["Groups"] = sg_ids
            kwargs["NetworkInterfaces"] = [nic]
        else:
            if subnet_id:
                kwargs["SubnetId"] = subnet_id
        if sg_ids and "NetworkInterfaces" not in kwargs:
            kwargs["SecurityGroupIds"] = sg_ids

        profile_name = cfg.get("ec2_iam_instance_profile") or cfg.get("iam_instance_profile")
        if profile_name:
            kwargs["IamInstanceProfile"] = {"Name": profile_name}

        user_data = cfg.get("ec2_user_data")
        if user_data:
            kwargs["UserData"] = str(user_data)

        tags = self._build_tags(task)
        kwargs["TagSpecifications"] = [
            {"ResourceType": "instance", "Tags": tags},
            {
                "ResourceType": "volume",
                "Tags": [tag for tag in tags if tag["Key"] not in {"Name", "TaskId"}],
            },
        ]

        runtime_logger.info("Launching EC2 sandbox in %s for task %s", self.region, task.get("task_name"))
        response = self.ec2.run_instances(**kwargs)
        instance = response["Instances"][0]
        instance_id = instance["InstanceId"]
        runtime_logger.info("Launched EC2 instance %s", instance_id)
        return instance_id

    def _build_tags(self, task: Dict[str, Any]) -> List[Dict[str, str]]:
        cfg = self.client.sandbox_config
        tags: Dict[str, str] = {
            "Name": str(cfg.get("ec2_instance_name") or f"uda-gym-{task.get('task_name', 'task')}"),
            "Project": str(cfg.get("ec2_project_tag") or "UDA-Gym"),
            "ManagedBy": "NanoRollout",
            "Runtime": "ec2",
            "Bench": str(cfg.get("bench") or "uda"),
            "TaskId": str(task.get("task_id") or task.get("task_name") or "task"),
        }
        owner = cfg.get("ec2_owner") or cfg.get("owner")
        if owner:
            tags["Owner"] = str(owner)
        ttl = cfg.get("ec2_ttl")
        if ttl:
            tags["TTL"] = str(ttl)
        env_profile = cfg.get("ec2_env_profile") or cfg.get("env_profile")
        if env_profile:
            tags["Profile"] = str(env_profile)
        extra_tags = cfg.get("ec2_tags")
        if isinstance(extra_tags, dict):
            tags.update({str(k): str(v) for k, v in extra_tags.items()})
        return [{"Key": k[:128], "Value": v[:256]} for k, v in tags.items()]

    def _wait_for_instance_running(self, instance_id: str, timeout: int) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                response = self.ec2.describe_instances(InstanceIds=[instance_id])
            except Exception as exc:
                err = getattr(exc, "response", {}).get("Error", {})
                if err.get("Code") in {"InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"}:
                    time.sleep(5)
                    continue
                raise
            reservations = response.get("Reservations", [])
            instances = reservations[0].get("Instances", []) if reservations else []
            if not instances:
                time.sleep(5)
                continue
            instance = instances[0]
            state = instance.get("State", {}).get("Name")
            if state == "running":
                return instance
            if state in {"shutting-down", "terminated", "stopping", "stopped"}:
                raise RuntimeError(f"EC2 instance {instance_id} entered state {state}")
            time.sleep(5)
        raise TimeoutError(f"EC2 instance {instance_id} did not reach running in {timeout}s")

    def _select_instance_ip(self, instance: Dict[str, Any]) -> str:
        cfg = self.client.sandbox_config
        use_private = bool(cfg.get("ec2_use_private_ip", False))
        if use_private:
            return instance.get("PrivateIpAddress") or instance.get("PublicIpAddress") or ""
        return instance.get("PublicIpAddress") or instance.get("PrivateIpAddress") or ""

    def _clear_client_runtime(self) -> None:
        self.client.container_id = None
        self.client.runtime_id = None
        self.instance_id = None
        self.owns_instance = False
