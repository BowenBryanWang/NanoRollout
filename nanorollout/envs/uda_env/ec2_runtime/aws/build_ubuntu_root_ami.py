#!/usr/bin/env python3
"""Build a clean Ubuntu-based UDA EC2 root AMI."""

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
from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGION = "ap-southeast-1"
CANONICAL_OWNER = "099720109477"
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
    p.add_argument("--source-ami", default=None)
    p.add_argument("--ubuntu-release", default="jammy-22.04")
    p.add_argument("--architecture", default="x86_64")
    p.add_argument("--subnet-id", required=True)
    p.add_argument("--security-group-id", required=True)
    p.add_argument("--instance-profile", default="uda-gym-ec2-instance-profile")
    p.add_argument("--instance-type", default="t3.large")
    p.add_argument("--volume-size", type=int, default=80)
    p.add_argument("--key-name", default=None)
    p.add_argument("--launch-template-id", default=None)
    p.add_argument("--launch-template-name", default=None)
    p.add_argument("--ami-name", default=None)
    p.add_argument("--timeout", type=int, default=7200)
    p.add_argument("--keep-builder", action="store_true")
    p.add_argument("--allow-passwordless-sudo", action="store_true")
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
        {"Key": "Purpose", "Value": "uda-ubuntu-root-ami"},
    ]


def latest_ubuntu_ami(ec2, release: str, architecture: str) -> str:
    name_arch = {"x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(
        architecture, architecture
    )
    ec2_arch = {"aarch64": "arm64"}.get(architecture, architecture)
    pattern = f"ubuntu/images/hvm-ssd/ubuntu-{release}-{name_arch}-server-*"
    images = ec2.describe_images(
        Owners=[CANONICAL_OWNER],
        Filters=[
            {"Name": "name", "Values": [pattern]},
            {"Name": "architecture", "Values": [ec2_arch]},
            {"Name": "root-device-type", "Values": ["ebs"]},
            {"Name": "virtualization-type", "Values": ["hvm"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )["Images"]
    if not images:
        raise RuntimeError(f"No Ubuntu AMI matched {pattern!r}")
    image = sorted(images, key=lambda item: item["CreationDate"])[-1]
    print(json.dumps({
        "ubuntu_source_ami": image["ImageId"],
        "name": image.get("Name"),
        "creation_date": image.get("CreationDate"),
    }, indent=2))
    return image["ImageId"]


def inline_user_data(args: argparse.Namespace) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for local_name in ["uda_compat_server.py", "provision_ubuntu_root_ami.sh"]:
            data = (ROOT / local_name).read_bytes()
            info = tarfile.TarInfo(local_name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    payload = base64.b64encode(buf.getvalue()).decode("ascii")
    sudo_flag = "true" if args.allow_passwordless_sudo else "false"
    script = f"""#!/usr/bin/env bash
set -euxo pipefail
cat >/tmp/uda_root_layer.tgz.b64 <<'EOF'
{payload}
EOF
base64 -d /tmp/uda_root_layer.tgz.b64 >/tmp/uda_root_layer.tgz
mkdir -p /tmp/uda_root_layer
tar -xzf /tmp/uda_root_layer.tgz -C /tmp/uda_root_layer
UDA_ALLOW_PASSWORDLESS_SUDO={sudo_flag} bash /tmp/uda_root_layer/provision_ubuntu_root_ami.sh
"""
    if len(script.encode("utf-8")) > 16 * 1024:
        raise ValueError("inline user-data exceeds the EC2 16KB limit")
    return script


def launch_builder(ec2, args: argparse.Namespace, image_id: str) -> str:
    resp = ec2.run_instances(
        ImageId=image_id,
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
        UserData=inline_user_data(args),
        **({"KeyName": args.key_name} if args.key_name else {}),
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": tags("uda-ubuntu-root-ami-builder", "builder")},
            {"ResourceType": "volume", "Tags": tags("uda-ubuntu-root-ami-builder-volume", "builder")},
        ],
    )
    iid = resp["Instances"][0]["InstanceId"]
    print(f"Launched Ubuntu root AMI builder {iid}")
    return iid


def instance_public_ip(ec2, instance_id: str) -> str | None:
    inst = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    return inst.get("PublicIpAddress")


def wait_http_health(ec2, instance_id: str, timeout: int) -> str:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        public_ip = instance_public_ip(ec2, instance_id)
        if not public_ip:
            last_error = "no public IP yet"
        else:
            url = f"http://{public_ip}:8080/v1/sandbox"
            try:
                with DIRECT_OPENER.open(url, timeout=5) as resp:
                    body = resp.read(4096).decode("utf-8", "replace")
                parsed = json.loads(body)
                if parsed.get("success") or parsed.get("version") or parsed.get("data") is None:
                    print(f"UDA /v1 health online for {instance_id} at {url}")
                    return url
                last_error = f"unexpected body: {body[:200]}"
            except (OSError, ValueError, error.HTTPError, error.URLError) as exc:
                last_error = str(exc)
        print(f"Waiting for UDA /v1 health on {instance_id}: {last_error}")
        time.sleep(20)
    raise TimeoutError(f"UDA /v1 health did not come online for {instance_id}: {last_error}")


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
    name = args.ami_name or f"uda-ubuntu-root-general-{int(time.time())}"
    resp = ec2.create_image(
        InstanceId=instance_id,
        Name=name,
        Description="UDA general-base root AMI built from clean Ubuntu",
        NoReboot=False,
        TagSpecifications=[{"ResourceType": "image", "Tags": tags(name, "general-root")}],
    )
    image_id = resp["ImageId"]
    print(f"Creating UDA Ubuntu root AMI {image_id} ({name})")
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


def update_launch_template(ec2, args: argparse.Namespace, image_id: str):
    if not args.launch_template_id and not args.launch_template_name:
        return None
    lt = {}
    if args.launch_template_id:
        lt["LaunchTemplateId"] = args.launch_template_id
    if args.launch_template_name:
        lt["LaunchTemplateName"] = args.launch_template_name
    resp = ec2.create_launch_template_version(
        **lt,
        VersionDescription=f"uda-ubuntu-root-general {image_id}",
        SourceVersion="$Default",
        LaunchTemplateData={"ImageId": image_id},
    )
    version = resp["LaunchTemplateVersion"]["VersionNumber"]
    ec2.modify_launch_template(**lt, DefaultVersion=str(version))
    return version


def main() -> int:
    args = parse_args()
    bypass_proxy_env()
    sess = session(args.profile, args.region)
    ec2 = sess.client("ec2")
    source_ami = args.source_ami or latest_ubuntu_ami(ec2, args.ubuntu_release, args.architecture)
    builder = launch_builder(ec2, args, source_ami)
    try:
        ec2.get_waiter("instance_running").wait(InstanceIds=[builder])
        wait_http_health(ec2, builder, args.timeout)
        image_id = create_ami(ec2, args, builder)
        version = update_launch_template(ec2, args, image_id)
        print(json.dumps({
            "uda_ami_id": image_id,
            "source_ami_id": source_ami,
            "launch_template_default_version": version,
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
            except ClientError as exc:
                print(f"WARNING: failed to terminate builder {builder}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
