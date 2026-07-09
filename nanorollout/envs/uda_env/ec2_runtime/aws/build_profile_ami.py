#!/usr/bin/env python3
"""Build a profile AMI derived from an existing UDA EC2 base AMI."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import tarfile
import time
from pathlib import Path
from urllib import error, request

import boto3


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGION = "ap-southeast-1"
DIRECT_OPENER = request.build_opener(request.ProxyHandler({}))


def bypass_proxy_env() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default=None)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--source-ami", required=True)
    p.add_argument("--profile-name", required=True)
    p.add_argument("--provision-script", required=True)
    p.add_argument("--subnet-id", required=True)
    p.add_argument("--security-group-id", required=True)
    p.add_argument("--instance-profile", default="uda-gym-ec2-instance-profile")
    p.add_argument("--instance-type", default="t3.xlarge")
    p.add_argument("--volume-size", type=int, default=120)
    p.add_argument("--key-name", default=None)
    p.add_argument("--ami-name", default=None)
    p.add_argument("--timeout", type=int, default=7200)
    p.add_argument(
        "--provision-transport",
        choices=("ssm", "user-data"),
        default="ssm",
        help="How to run the profile provision script. SSM is the validated path for OSWorld-derived AMIs.",
    )
    p.add_argument("--keep-builder", action="store_true")
    return p.parse_args()


def session(profile: str | None, region: str):
    kwargs = {"region_name": region}
    if profile:
        kwargs["profile_name"] = profile
    return boto3.Session(**kwargs)


def tags(name: str, profile: str):
    return [
        {"Key": "Name", "Value": name},
        {"Key": "Project", "Value": "UDA-Gym"},
        {"Key": "ManagedBy", "Value": "Codex"},
        {"Key": "Runtime", "Value": "ec2"},
        {"Key": "Profile", "Value": profile},
        {"Key": "Purpose", "Value": "uda-profile-ami"},
    ]


def inline_user_data(args: argparse.Namespace) -> str:
    script_path = Path(args.provision_script).expanduser().resolve()
    data = script_path.read_bytes()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(script_path.name)
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))
    payload = base64.b64encode(buf.getvalue()).decode("ascii")
    script = f"""#!/usr/bin/env bash
set -euxo pipefail
cat >/tmp/uda_profile_layer.tgz.b64 <<'EOF'
{payload}
EOF
base64 -d /tmp/uda_profile_layer.tgz.b64 >/tmp/uda_profile_layer.tgz
mkdir -p /tmp/uda_profile_layer
tar -xzf /tmp/uda_profile_layer.tgz -C /tmp/uda_profile_layer
UDA_PROFILE_NAME={args.profile_name} bash /tmp/uda_profile_layer/{script_path.name}
"""
    script = script.replace(
        f"UDA_PROFILE_NAME={args.profile_name} bash",
        (
            f"rm -f /opt/uda-ec2/profile-{args.profile_name}.ready "
            f"/opt/uda-ec2/profile-{args.profile_name}.json\n"
            f"UDA_PROFILE_NAME={args.profile_name} bash"
        ),
    )
    if len(script.encode("utf-8")) > 16 * 1024:
        raise ValueError("inline user-data exceeds the EC2 16KB limit")
    return script


def launch_builder(ec2, args: argparse.Namespace) -> str:
    name = f"uda-{args.profile_name}-profile-ami-builder"
    run_kwargs = {}
    if args.provision_transport == "user-data":
        run_kwargs["UserData"] = inline_user_data(args)
    resp = ec2.run_instances(
        ImageId=args.source_ami,
        InstanceType=args.instance_type,
        MinCount=1,
        MaxCount=1,
        IamInstanceProfile={"Name": args.instance_profile},
        NetworkInterfaces=[{
            "SubnetId": args.subnet_id,
            "DeviceIndex": 0,
            "AssociatePublicIpAddress": True,
            "Groups": [args.security_group_id],
        }],
        BlockDeviceMappings=[{
            "DeviceName": "/dev/sda1",
            "Ebs": {
                "VolumeSize": args.volume_size,
                "VolumeType": "gp3",
                "DeleteOnTermination": True,
                "Encrypted": True,
            },
        }],
        **run_kwargs,
        **({"KeyName": args.key_name} if args.key_name else {}),
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": tags(name, args.profile_name)},
            {"ResourceType": "volume", "Tags": tags(f"{name}-volume", args.profile_name)},
        ],
    )
    iid = resp["Instances"][0]["InstanceId"]
    print(f"Launched profile AMI builder {iid}")
    return iid


def wait_ssm_online(ssm, instance_id: str, timeout: int) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            info = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}],
            ).get("InstanceInformationList", [])
            if info and info[0].get("PingStatus") == "Online":
                print(f"SSM online for {instance_id}")
                return
            last_error = info[0].get("PingStatus") if info else "not registered"
        except Exception as exc:
            last_error = str(exc)
        print(f"Waiting for SSM on {instance_id}: {last_error}")
        time.sleep(10)
    raise TimeoutError(f"SSM did not become online for {instance_id}: {last_error}")


def wait_ssm_command(ssm, instance_id: str, command_id: str, timeout: int) -> None:
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        try:
            invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(5)
            continue
        status = invocation["Status"]
        if status != last_status:
            stdout = invocation.get("StandardOutputContent", "")[-2000:]
            stderr = invocation.get("StandardErrorContent", "")[-2000:]
            print(json.dumps({
                "ssm_command_id": command_id,
                "status": status,
                "stdout_tail": stdout,
                "stderr_tail": stderr,
            }, indent=2))
            last_status = status
        if status == "Success":
            return
        if status in {"Cancelled", "TimedOut", "Failed", "Cancelling"}:
            stdout = invocation.get("StandardOutputContent", "")[-6000:]
            stderr = invocation.get("StandardErrorContent", "")[-6000:]
            raise RuntimeError(
                f"SSM provision command {command_id} failed with {status}\n"
                f"STDOUT tail:\n{stdout}\nSTDERR tail:\n{stderr}"
            )
        time.sleep(30)
    raise TimeoutError(f"SSM command {command_id} timed out")


def run_profile_provision_via_ssm(ssm, args: argparse.Namespace, instance_id: str) -> None:
    script_path = Path(args.provision_script).expanduser().resolve()
    payload = base64.b64encode(script_path.read_bytes()).decode("ascii")
    marker = f"/opt/uda-ec2/profile-{args.profile_name}"
    command = f"""bash <<'BASH'
set -euxo pipefail
cat >/tmp/uda_profile_layer.sh.b64 <<'EOF'
{payload}
EOF
base64 -d /tmp/uda_profile_layer.sh.b64 >/tmp/uda_profile_layer.sh
chmod 0755 /tmp/uda_profile_layer.sh
rm -f {marker}.ready {marker}.json
UDA_PROFILE_NAME={args.profile_name} bash /tmp/uda_profile_layer.sh
BASH
"""
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command], "executionTimeout": [str(args.timeout)]},
    )
    command_id = response["Command"]["CommandId"]
    print(f"Started SSM profile provision command {command_id}")
    wait_ssm_command(ssm, instance_id, command_id, args.timeout)


def instance_public_ip(ec2, instance_id: str) -> str | None:
    inst = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    return inst.get("PublicIpAddress")


def post_json(url: str, payload: dict, timeout: int = 10) -> dict:
    raw = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=raw, headers={"content-type": "application/json"})
    with DIRECT_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def wait_profile_ready(ec2, instance_id: str, profile_name: str, timeout: int) -> str:
    deadline = time.time() + timeout
    last_error = None
    session_id = None
    while time.time() < deadline:
        public_ip = instance_public_ip(ec2, instance_id)
        if not public_ip:
            last_error = "no public IP yet"
        else:
            base = f"http://{public_ip}:8080"
            try:
                if session_id is None:
                    created = post_json(f"{base}/v1/shell/sessions/create", {"exec_dir": "/home/user"})
                    session_id = (created.get("data") or {}).get("session_id")
                cmd = (
                    f"test -f /opt/uda-ec2/profile-{profile_name}.ready "
                    f"&& cat /opt/uda-ec2/profile-{profile_name}.json "
                    "&& echo __UDA_PROFILE_READY__"
                )
                result = post_json(
                    f"{base}/v1/shell/exec",
                    {"id": session_id, "command": cmd, "exec_dir": "/home/user", "timeout": 20},
                    timeout=30,
                )
                output = ((result.get("data") or {}).get("output") or "")
                if "__UDA_PROFILE_READY__" in output:
                    print(f"UDA profile {profile_name} ready for {instance_id} at {base}")
                    return base
                last_error = output[-500:] or "profile marker not ready"
            except (OSError, ValueError, error.HTTPError, error.URLError) as exc:
                last_error = str(exc)
        print(f"Waiting for UDA profile {profile_name} on {instance_id}: {last_error}")
        time.sleep(30)
    raise TimeoutError(f"UDA profile {profile_name} did not become ready for {instance_id}: {last_error}")


def wait_image(ec2, image_id: str, timeout: int):
    deadline = time.time() + timeout
    while time.time() < deadline:
        img = ec2.describe_images(ImageIds=[image_id])["Images"][0]
        state = img["State"]
        if state == "available":
            return img
        if state in {"failed", "deregistered", "error"}:
            raise RuntimeError(f"AMI {image_id} entered state {state}")
        print(f"AMI {image_id} state={state}; waiting...")
        time.sleep(30)
    raise TimeoutError(f"Timed out waiting for AMI {image_id}")


def create_ami(ec2, args: argparse.Namespace, instance_id: str) -> str:
    name = args.ami_name or f"uda-{args.profile_name}-profile-{int(time.time())}"
    resp = ec2.create_image(
        InstanceId=instance_id,
        Name=name,
        Description=f"UDA {args.profile_name} profile AMI",
        NoReboot=False,
        TagSpecifications=[{"ResourceType": "image", "Tags": tags(name, args.profile_name)}],
    )
    image_id = resp["ImageId"]
    print(f"Creating UDA profile AMI {image_id} ({name})")
    wait_image(ec2, image_id, args.timeout)
    return image_id


def print_console_output(ec2, instance_id: str) -> None:
    try:
        output = ec2.get_console_output(InstanceId=instance_id, Latest=True).get("Output") or ""
    except Exception as exc:
        print(f"WARNING: failed to fetch console output for {instance_id}: {exc}")
        return
    if output:
        print(f"----- EC2 console output for {instance_id} -----")
        print(output[-12000:])
        print("----- end EC2 console output -----")


def main() -> int:
    args = parse_args()
    bypass_proxy_env()
    aws_session = session(args.profile, args.region)
    ec2 = aws_session.client("ec2")
    ssm = aws_session.client("ssm")
    builder = launch_builder(ec2, args)
    try:
        ec2.get_waiter("instance_running").wait(InstanceIds=[builder])
        if args.provision_transport == "ssm":
            wait_ssm_online(ssm, builder, min(args.timeout, 900))
            run_profile_provision_via_ssm(ssm, args, builder)
        wait_profile_ready(ec2, builder, args.profile_name, args.timeout)
        image_id = create_ami(ec2, args, builder)
        print(json.dumps({
            "profile": args.profile_name,
            "source_ami": args.source_ami,
            "image_id": image_id,
            "region": args.region,
            "builder_instance_id": builder,
            "provision_transport": args.provision_transport,
        }, indent=2))
        return 0
    except Exception:
        print_console_output(ec2, builder)
        raise
    finally:
        if not args.keep_builder:
            try:
                ec2.terminate_instances(InstanceIds=[builder])
                print(f"Terminated builder {builder}")
            except Exception as exc:
                print(f"WARNING: failed to terminate builder {builder}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
