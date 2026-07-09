#!/usr/bin/env python3
"""Build a UDA EC2 AMI by layering /v1 APIs onto the OSWorld image."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shlex
import subprocess
import tarfile
import time
from pathlib import Path
from urllib import error, request

import boto3
from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_AMI = "ami-0d23263edb96951d8"
DEFAULT_SOURCE_REGION = "us-east-1"
DEFAULT_REGION = "ap-southeast-1"
DIRECT_OPENER = request.build_opener(request.ProxyHandler({}))


def bypass_proxy_env():
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default=None)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--source-region", default=DEFAULT_SOURCE_REGION)
    p.add_argument("--source-ami", default=DEFAULT_SOURCE_AMI)
    p.add_argument("--subnet-id", required=True)
    p.add_argument("--security-group-id", required=True)
    p.add_argument("--instance-profile", default="uda-gym-ec2-instance-profile")
    p.add_argument("--instance-type", default="m7i.large")
    p.add_argument("--key-name", default=None)
    p.add_argument("--private-key-path", default=None)
    p.add_argument("--ssh-user", default=None)
    p.add_argument("--ssh-users", default="user,ubuntu,ec2-user")
    p.add_argument("--launch-template-id", default=None)
    p.add_argument("--launch-template-name", default=None)
    p.add_argument("--ami-name", default=None)
    p.add_argument("--copy-name", default=None)
    p.add_argument("--provision-method", choices=["user-data", "ssm", "ssh", "osworld-http"], default="user-data")
    p.add_argument("--osworld-port", type=int, default=5000)
    p.add_argument("--osworld-sudo-password", default="osworld-public-evaluation")
    p.add_argument("--timeout", type=int, default=7200)
    p.add_argument("--keep-builder", action="store_true")
    return p.parse_args()


def session(profile, region):
    kwargs = {"region_name": region}
    if profile:
        kwargs["profile_name"] = profile
    return boto3.Session(**kwargs)


def wait_image(ec2, image_id, timeout):
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


def copy_source_ami(ec2, args):
    if args.source_region == args.region:
        return args.source_ami
    name = args.copy_name or f"osworld-client-uda-source-{args.source_ami}-{args.region}"
    existing = ec2.describe_images(
        Owners=["self"],
        Filters=[{"Name": "name", "Values": [name]}, {"Name": "state", "Values": ["pending", "available"]}],
    )["Images"]
    if existing:
        image_id = sorted(existing, key=lambda x: x["CreationDate"])[-1]["ImageId"]
        print(f"Reusing copied source AMI {image_id} ({name})")
        wait_image(ec2, image_id, args.timeout)
        return image_id
    resp = ec2.copy_image(
        SourceRegion=args.source_region,
        SourceImageId=args.source_ami,
        Name=name,
        Description=f"Copy of OSWorld source {args.source_ami} for UDA EC2",
        Encrypted=True,
        TagSpecifications=[{"ResourceType": "image", "Tags": tags("uda-osworld-source-copy", "source")}],
    )
    image_id = resp["ImageId"]
    print(f"Started AMI copy: {image_id}")
    wait_image(ec2, image_id, args.timeout)
    return image_id


def tags(name, profile):
    return [
        {"Key": "Name", "Value": name},
        {"Key": "Project", "Value": "UDA-Gym"},
        {"Key": "ManagedBy", "Value": "Codex"},
        {"Key": "Runtime", "Value": "ec2"},
        {"Key": "Profile", "Value": profile},
    ]


def ssm_user_data():
    return """#!/usr/bin/env bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends snapd curl ca-certificates
systemctl enable --now snapd.socket || true
snap install amazon-ssm-agent --classic || true
systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service || true
"""


def inline_layer_user_data():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for local_name in ["uda_compat_server.py", "provision_uda_layer.sh"]:
            data = (ROOT / local_name).read_bytes()
            info = tarfile.TarInfo(local_name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    payload = base64.b64encode(buf.getvalue()).decode("ascii")
    script = f"""#!/usr/bin/env bash
set -euxo pipefail
cat >/tmp/uda_layer.tgz.b64 <<'EOF'
{payload}
EOF
base64 -d /tmp/uda_layer.tgz.b64 >/tmp/uda_layer.tgz
mkdir -p /tmp/uda_layer
tar -xzf /tmp/uda_layer.tgz -C /tmp/uda_layer
bash /tmp/uda_layer/provision_uda_layer.sh >/var/log/uda-provision.log 2>&1
"""
    if len(script.encode("utf-8")) > 16 * 1024:
        raise ValueError("inline user-data exceeds the EC2 16KB limit")
    return script


def launch_builder(ec2, args, image_id):
    if args.provision_method == "ssm":
        builder_user_data = ssm_user_data()
    elif args.provision_method == "user-data":
        builder_user_data = inline_layer_user_data()
    else:
        builder_user_data = ""
    key_name = args.key_name
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
                "VolumeSize": 80,
                "VolumeType": "gp3",
                "DeleteOnTermination": True,
                "Encrypted": True,
            },
        }],
        UserData=builder_user_data,
        **({"KeyName": key_name} if key_name else {}),
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": tags("uda-ec2-ami-builder", "builder")},
            {"ResourceType": "volume", "Tags": tags("uda-ec2-ami-builder-volume", "builder")},
        ],
    )
    iid = resp["Instances"][0]["InstanceId"]
    print(f"Launched builder {iid}")
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    return iid


def instance_public_ip(ec2, instance_id):
    inst = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    return inst.get("PublicIpAddress")


def ssh_base_cmd(args, user, host):
    if not args.private_key_path:
        raise ValueError("--private-key-path is required for --provision-method ssh")
    key_path = str(Path(args.private_key_path).expanduser())
    return [
        "ssh",
        "-i", key_path,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{user}@{host}",
    ]


def scp_base_cmd(args):
    if not args.private_key_path:
        raise ValueError("--private-key-path is required for --provision-method ssh")
    key_path = str(Path(args.private_key_path).expanduser())
    return [
        "scp",
        "-i", key_path,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
    ]


def wait_ssh(ec2, args, instance_id, timeout):
    deadline = time.time() + timeout
    users = [args.ssh_user] if args.ssh_user else [
        part.strip() for part in str(args.ssh_users).split(",") if part.strip()
    ]
    last_error = None
    while time.time() < deadline:
        public_ip = instance_public_ip(ec2, instance_id)
        if not public_ip:
            print(f"Waiting for public IP on {instance_id}...")
            time.sleep(10)
            continue
        for user in users:
            probe = ssh_base_cmd(args, user, public_ip) + ["true"]
            proc = subprocess.run(probe, text=True, capture_output=True, timeout=20)
            if proc.returncode == 0:
                print(f"SSH online for {instance_id} as {user}@{public_ip}")
                return user, public_ip
            last_error = (proc.stderr or proc.stdout or "").strip()
        print(f"Waiting for SSH on {instance_id}: {last_error[:240]}")
        time.sleep(15)
    raise TimeoutError(f"SSH did not come online for {instance_id}: {last_error}")


def ssh_run(args, user, host, command, timeout=1800):
    proc = subprocess.run(
        ssh_base_cmd(args, user, host) + [command],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.stdout:
        print(proc.stdout)
    if proc.returncode != 0:
        if proc.stderr:
            print(proc.stderr)
        raise RuntimeError(f"SSH command failed with exit code {proc.returncode}: {command}")
    return proc


def install_layer_ssh(ec2, args, instance_id):
    user, host = wait_ssh(ec2, args, instance_id, min(args.timeout, 1800))
    server = ROOT / "uda_compat_server.py"
    provision = ROOT / "provision_uda_layer.sh"
    subprocess.run(
        scp_base_cmd(args)
        + [str(server), str(provision), f"{user}@{host}:/tmp/"],
        check=True,
        text=True,
        timeout=120,
    )
    ssh_run(args, user, host, "chmod +x /tmp/provision_uda_layer.sh /tmp/uda_compat_server.py", timeout=60)
    ssh_run(args, user, host, "sudo -n bash /tmp/provision_uda_layer.sh", timeout=args.timeout)
    wait_http_health(ec2, instance_id, min(args.timeout, 900))


def wait_http_health(ec2, instance_id, timeout):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        public_ip = instance_public_ip(ec2, instance_id)
        if not public_ip:
            print(f"Waiting for public IP on {instance_id}...")
            time.sleep(10)
            continue
        url = f"http://{public_ip}:8080/v1/sandbox"
        try:
            with DIRECT_OPENER.open(url, timeout=5) as resp:
                body = resp.read(4096).decode("utf-8", "replace")
            parsed = json.loads(body)
            if parsed.get("sandbox_id") or parsed.get("success") or parsed.get("version"):
                print(f"UDA /v1 health online for {instance_id} at {url}")
                return url
            last_error = f"unexpected body: {body[:200]}"
        except (OSError, ValueError, error.HTTPError, error.URLError) as exc:
            last_error = str(exc)
        print(f"Waiting for UDA /v1 health on {instance_id}: {last_error}")
        time.sleep(20)
    raise TimeoutError(f"UDA /v1 health did not come online for {instance_id}: {last_error}")


def http_json(method, url, payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with DIRECT_OPENER.open(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if body else {}


def wait_osworld_http(ec2, args, instance_id, timeout):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        public_ip = instance_public_ip(ec2, instance_id)
        if not public_ip:
            print(f"Waiting for public IP on {instance_id}...")
            time.sleep(10)
            continue
        base_url = f"http://{public_ip}:{args.osworld_port}"
        try:
            info = http_json("GET", f"{base_url}/terminal", timeout=5)
            if info.get("status") == "success":
                print(f"OSWorld HTTP online for {instance_id} at {base_url}")
                return base_url
        except (OSError, ValueError, error.HTTPError, error.URLError) as exc:
            last_error = str(exc)
        print(f"Waiting for OSWorld HTTP on {instance_id}: {last_error}")
        time.sleep(20)
    raise TimeoutError(f"OSWorld HTTP did not come online for {instance_id}: {last_error}")


def osworld_execute(base_url, command, *, shell=False, timeout=125):
    result = http_json(
        "POST",
        f"{base_url}/execute",
        {"command": command, "shell": shell},
        timeout=timeout,
    )
    if result.get("status") != "success" or int(result.get("returncode") or 0) != 0:
        raise RuntimeError(f"OSWorld execute failed: {result}")
    return result


def osworld_read_file(base_url, path):
    return http_json("POST", f"{base_url}/file", {"file_path": path}, timeout=30)


def osworld_start_bash(base_url, command):
    result = osworld_execute(base_url, command, shell=True)
    if result.get("output"):
        print(result["output"])
    if result.get("error"):
        print(result["error"])
    return result


def install_layer_osworld_http(ec2, args, instance_id):
    base_url = wait_osworld_http(ec2, args, instance_id, min(args.timeout, 1800))
    server_b64 = base64.b64encode((ROOT / "uda_compat_server.py").read_bytes()).decode("ascii")
    provision_b64 = base64.b64encode((ROOT / "provision_uda_layer.sh").read_bytes()).decode("ascii")
    code = f"""
import base64
from pathlib import Path
Path('/tmp/uda_compat_server.py').write_bytes(base64.b64decode({server_b64!r}))
Path('/tmp/provision_uda_layer.sh').write_bytes(base64.b64decode({provision_b64!r}))
Path('/tmp/uda_compat_server.py').chmod(0o755)
Path('/tmp/provision_uda_layer.sh').chmod(0o755)
print('wrote uda layer files')
"""
    osworld_execute(base_url, ["/usr/bin/python3", "-c", code])
    osworld_start_bash(
        base_url,
        (
            "nohup bash -lc "
            + json.dumps(
                "printf '%s\\n' "
                + shlex.quote(args.osworld_sudo_password)
                + " | sudo -S bash /tmp/provision_uda_layer.sh >/tmp/uda-provision.log 2>&1"
            )
            + " >/tmp/uda-provision-start.log 2>&1 &"
        ),
    )
    try:
        wait_http_health(ec2, instance_id, min(args.timeout, 1800))
    except Exception:
        try:
            print(osworld_read_file(base_url, "/tmp/uda-provision.log"))
        except Exception as log_exc:
            print(f"WARNING: failed to fetch provision log: {log_exc}")
        raise


def wait_ssm(ssm, instance_id, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        infos = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        ).get("InstanceInformationList", [])
        if infos and infos[0].get("PingStatus") == "Online":
            print(f"SSM online for {instance_id}")
            return
        print(f"Waiting for SSM on {instance_id}...")
        time.sleep(15)
    raise TimeoutError(f"SSM did not come online for {instance_id}")


def ssm_run(ssm, instance_id, commands, timeout=1800):
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
    )
    cid = resp["Command"]["CommandId"]
    while True:
        inv = ssm.get_command_invocation(CommandId=cid, InstanceId=instance_id)
        status = inv["Status"]
        if status in {"Success", "Cancelled", "Failed", "TimedOut", "Cancelling"}:
            print(inv.get("StandardOutputContent", ""))
            if status != "Success":
                print(inv.get("StandardErrorContent", ""))
                raise RuntimeError(f"SSM command {cid} failed: {status}")
            return inv
        time.sleep(5)


def put_file_commands(local_path, remote_path):
    data = base64.b64encode(Path(local_path).read_bytes()).decode("ascii")
    chunks = [data[i:i + 7000] for i in range(0, len(data), 7000)]
    cmds = [f"rm -f {remote_path}.b64 {remote_path}"]
    for chunk in chunks:
        cmds.append(f"cat >> {remote_path}.b64 <<'EOF'\n{chunk}\nEOF")
    cmds.extend([
        f"base64 -d {remote_path}.b64 > {remote_path}",
        f"chmod +x {remote_path}",
        f"rm -f {remote_path}.b64",
    ])
    return cmds


def install_layer(ssm, instance_id):
    server = ROOT / "uda_compat_server.py"
    provision = ROOT / "provision_uda_layer.sh"
    cmds = []
    cmds += put_file_commands(server, "/tmp/uda_compat_server.py")
    cmds += put_file_commands(provision, "/tmp/provision_uda_layer.sh")
    cmds.append("bash /tmp/provision_uda_layer.sh")
    cmds.append("curl -fsS http://127.0.0.1:8080/v1/sandbox")
    ssm_run(ssm, instance_id, cmds)


def create_uda_ami(ec2, args, instance_id):
    name = args.ami_name or f"uda-general-base-osworld-{int(time.time())}"
    resp = ec2.create_image(
        InstanceId=instance_id,
        Name=name,
        Description="UDA general-base AMI layered on OSWorld verified image",
        NoReboot=False,
        TagSpecifications=[{"ResourceType": "image", "Tags": tags(name, "general")}],
    )
    image_id = resp["ImageId"]
    print(f"Creating UDA AMI {image_id} ({name})")
    wait_image(ec2, image_id, args.timeout)
    return image_id


def update_launch_template(ec2, args, image_id):
    if not args.launch_template_id and not args.launch_template_name:
        return None
    lt = {}
    if args.launch_template_id:
        lt["LaunchTemplateId"] = args.launch_template_id
    if args.launch_template_name:
        lt["LaunchTemplateName"] = args.launch_template_name
    resp = ec2.create_launch_template_version(
        **lt,
        VersionDescription=f"uda-general-base {image_id}",
        SourceVersion="$Default",
        LaunchTemplateData={"ImageId": image_id},
    )
    version = resp["LaunchTemplateVersion"]["VersionNumber"]
    ec2.modify_launch_template(**lt, DefaultVersion=str(version))
    return version


def main():
    args = parse_args()
    bypass_proxy_env()
    sess = session(args.profile, args.region)
    ec2 = sess.client("ec2")
    ssm = sess.client("ssm") if args.provision_method == "ssm" else None
    source = copy_source_ami(ec2, args)
    builder = launch_builder(ec2, args, source)
    try:
        if args.provision_method == "ssm":
            wait_ssm(ssm, builder, min(args.timeout, 1800))
            install_layer(ssm, builder)
        elif args.provision_method == "ssh":
            install_layer_ssh(ec2, args, builder)
        elif args.provision_method == "osworld-http":
            install_layer_osworld_http(ec2, args, builder)
        else:
            wait_http_health(ec2, builder, args.timeout)
        image_id = create_uda_ami(ec2, args, builder)
        version = update_launch_template(ec2, args, image_id)
        print(json.dumps({"uda_ami_id": image_id, "launch_template_default_version": version}, indent=2))
    finally:
        if not args.keep_builder:
            try:
                ec2.terminate_instances(InstanceIds=[builder])
                print(f"Terminated builder {builder}")
            except ClientError as exc:
                print(f"WARNING: failed to terminate builder {builder}: {exc}")


if __name__ == "__main__":
    main()
