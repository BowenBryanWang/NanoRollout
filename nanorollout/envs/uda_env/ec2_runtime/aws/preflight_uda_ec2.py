#!/usr/bin/env python3
"""Preflight checks for the UDA EC2 runtime deployment."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_REGION = "ap-southeast-1"
DEFAULT_LAUNCH_TEMPLATE_ID = "lt-03862713037af59fe"
DEFAULT_AMI_ID = "ami-0e0244ba3257d200d"
DEFAULT_SUBNET_ID = "subnet-0c85c17f888605401"
DEFAULT_SECURITY_GROUP_ID = "sg-029a65325aae8f739"
DEFAULT_INSTANCE_PROFILE = "uda-gym-ec2-instance-profile"
DEFAULT_INSTANCE_TYPE = "t3.xlarge"


def bypass_proxy_env() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default=None)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--launch-template-id", default=DEFAULT_LAUNCH_TEMPLATE_ID)
    p.add_argument("--expected-ami-id", default=DEFAULT_AMI_ID)
    p.add_argument("--subnet-id", default=DEFAULT_SUBNET_ID)
    p.add_argument("--security-group-id", default=DEFAULT_SECURITY_GROUP_ID)
    p.add_argument("--instance-profile", default=DEFAULT_INSTANCE_PROFILE)
    p.add_argument("--instance-type", default=DEFAULT_INSTANCE_TYPE)
    p.add_argument("--nanorollout-root", default=".")
    p.add_argument(
        "--nanorollout-python",
        default=None,
        help="Python interpreter to use for NanoRollout import/module checks.",
    )
    p.add_argument("--require-claude-auth", action="store_true")
    p.add_argument(
        "--skip-run-instances-dry-run",
        action="store_true",
        help="Skip the EC2 RunInstances DryRun permission/parameter check.",
    )
    return p.parse_args()


def status(ok: bool, detail=None) -> dict:
    return {"ok": bool(ok), "detail": detail}


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def run_python_check(python: Path, code: str, cwd: Path, env: dict[str, str]) -> dict:
    try:
        proc = subprocess.run(
            [str(python), "-c", code],
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
    detail = {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }
    return {"ok": proc.returncode == 0, "detail": detail}


def main() -> int:
    args = parse_args()
    bypass_proxy_env()
    results: dict[str, object] = {}
    failures: list[str] = []
    warnings: list[str] = []

    try:
        import boto3
    except Exception as exc:
        results["boto3"] = status(False, str(exc))
        failures.append("boto3")
        print(json.dumps({"results": results, "failures": failures}, indent=2))
        return 1

    session_kwargs = {"region_name": args.region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    sess = boto3.Session(**session_kwargs)
    ec2 = sess.client("ec2")
    iam = sess.client("iam")
    sts = sess.client("sts")

    try:
        ident = sts.get_caller_identity()
        results["aws_identity"] = status(True, ident)
    except Exception as exc:
        results["aws_identity"] = status(False, str(exc))
        failures.append("aws_identity")

    try:
        lt = ec2.describe_launch_template_versions(
            LaunchTemplateId=args.launch_template_id,
            Versions=["$Default"],
        )["LaunchTemplateVersions"][0]
        data = lt["LaunchTemplateData"]
        detail = {
            "default_version": lt["VersionNumber"],
            "image_id": data.get("ImageId"),
            "instance_type": data.get("InstanceType"),
        }
        ok = data.get("ImageId") == args.expected_ami_id
        results["launch_template"] = status(ok, detail)
        if not ok:
            failures.append("launch_template")
    except Exception as exc:
        results["launch_template"] = status(False, str(exc))
        failures.append("launch_template")

    try:
        img = ec2.describe_images(ImageIds=[args.expected_ami_id])["Images"][0]
        detail = {
            "image_id": img["ImageId"],
            "name": img.get("Name"),
            "state": img.get("State"),
            "creation_date": img.get("CreationDate"),
        }
        ok = img.get("State") == "available"
        results["ami"] = status(ok, detail)
        if not ok:
            failures.append("ami")
    except Exception as exc:
        results["ami"] = status(False, str(exc))
        failures.append("ami")

    try:
        subnet = ec2.describe_subnets(SubnetIds=[args.subnet_id])["Subnets"][0]
        results["subnet"] = status(
            subnet.get("State") == "available",
            {
                "subnet_id": subnet["SubnetId"],
                "az": subnet.get("AvailabilityZone"),
                "state": subnet.get("State"),
                "map_public_ip": subnet.get("MapPublicIpOnLaunch"),
            },
        )
        if subnet.get("State") != "available":
            failures.append("subnet")
    except Exception as exc:
        results["subnet"] = status(False, str(exc))
        failures.append("subnet")

    try:
        sg = ec2.describe_security_groups(GroupIds=[args.security_group_id])["SecurityGroups"][0]
        ports: dict[int, list[str]] = {}
        for perm in sg.get("IpPermissions", []):
            from_port = perm.get("FromPort")
            to_port = perm.get("ToPort")
            if from_port is None or to_port is None:
                continue
            for port in range(int(from_port), int(to_port) + 1):
                ports.setdefault(port, [])
                ports[port].extend(r.get("CidrIp", "") for r in perm.get("IpRanges", []))
        required_ports = [8080]
        ok = all(port in ports for port in required_ports)
        results["security_group"] = status(
            ok,
            {"group_id": sg["GroupId"], "name": sg.get("GroupName"), "ports": ports},
        )
        if not ok:
            failures.append("security_group")
    except Exception as exc:
        results["security_group"] = status(False, str(exc))
        failures.append("security_group")

    try:
        profile = iam.get_instance_profile(InstanceProfileName=args.instance_profile)
        results["instance_profile"] = status(
            True,
            {
                "name": args.instance_profile,
                "roles": [r["RoleName"] for r in profile["InstanceProfile"].get("Roles", [])],
            },
        )
    except Exception as exc:
        results["instance_profile"] = status(False, str(exc))
        failures.append("instance_profile")

    if args.skip_run_instances_dry_run:
        results["run_instances_dry_run"] = status(True, "skipped")
    else:
        try:
            ec2.run_instances(
                MinCount=1,
                MaxCount=1,
                LaunchTemplate={"LaunchTemplateId": args.launch_template_id, "Version": "$Default"},
                InstanceType=args.instance_type,
                SubnetId=args.subnet_id,
                SecurityGroupIds=[args.security_group_id],
                IamInstanceProfile={"Name": args.instance_profile},
                DryRun=True,
            )
            results["run_instances_dry_run"] = status(
                False,
                "RunInstances DryRun unexpectedly succeeded without DryRunOperation",
            )
            failures.append("run_instances_dry_run")
        except Exception as exc:
            err = getattr(exc, "response", {}).get("Error", {})
            code = err.get("Code")
            if code == "DryRunOperation":
                results["run_instances_dry_run"] = status(
                    True,
                    {
                        "code": code,
                        "message": err.get("Message"),
                        "launch_template_id": args.launch_template_id,
                        "subnet_id": args.subnet_id,
                        "security_group_id": args.security_group_id,
                        "instance_profile": args.instance_profile,
                        "instance_type": args.instance_type,
                    },
                )
            else:
                results["run_instances_dry_run"] = status(
                    False,
                    {"code": code, "message": err.get("Message") or str(exc)},
                )
                failures.append("run_instances_dry_run")

    nr_root = Path(args.nanorollout_root).expanduser().resolve()
    results["nanorollout_root"] = status(
        (nr_root / "nanorollout").is_dir(),
        str(nr_root),
    )
    if not (nr_root / "nanorollout").is_dir():
        failures.append("nanorollout_root")

    nr_python = Path(args.nanorollout_python).expanduser().absolute() if args.nanorollout_python else None
    if nr_python is None:
        candidate = nr_root / ".venv" / "bin" / "python"
        nr_python = candidate if candidate.exists() else Path(sys.executable)
    results["nanorollout_python"] = status(nr_python.exists(), str(nr_python))
    if not nr_python.exists():
        failures.append("nanorollout_python")

    nr_env = os.environ.copy()
    nr_env["PYTHONPATH"] = str(nr_root) + (
        os.pathsep + nr_env["PYTHONPATH"] if nr_env.get("PYTHONPATH") else ""
    )
    import_result = run_python_check(
        nr_python,
        "import nanorollout.envs.uda_env.ec2; print('ok')",
        nr_root,
        nr_env,
    )
    if import_result["ok"]:
        results["nanorollout_ec2_import"] = status(True, "ok")
    else:
        results["nanorollout_ec2_import"] = status(False, import_result["detail"])
        failures.append("nanorollout_ec2_import")

    auth_env = {
        "CLAUDE_CODE_OAUTH_TOKEN": bool(os.getenv("CLAUDE_CODE_OAUTH_TOKEN")),
        "ANTHROPIC_API_KEY": bool(os.getenv("ANTHROPIC_API_KEY")),
        "AWS_BEARER_TOKEN_BEDROCK": bool(os.getenv("AWS_BEARER_TOKEN_BEDROCK")),
    }
    has_claude_auth = any(auth_env.values())
    results["claude_auth"] = status(has_claude_auth, auth_env)
    if args.require_claude_auth and not has_claude_auth:
        failures.append("claude_auth")
    elif not has_claude_auth:
        warnings.append("claude_auth_missing")

    module_result = run_python_check(
        nr_python,
        (
            "import importlib.util, json; "
            "mods={m: importlib.util.find_spec(m) is not None for m in ['agent_sandbox','boto3']}; "
            "print(json.dumps(mods)); "
            "raise SystemExit(0 if all(mods.values()) else 1)"
        ),
        nr_root,
        nr_env,
    )
    modules = {}
    if module_result["detail"].get("stdout"):
        try:
            modules = json.loads(module_result["detail"]["stdout"])
        except Exception:
            modules = {"raw": module_result["detail"]["stdout"]}
    modules["preflight_boto3"] = has_module("boto3")
    results["python_modules"] = status(
        module_result["ok"] and has_module("boto3"),
        modules,
    )
    if not results["python_modules"]["ok"]:
        failures.append("python_modules")

    report = {"results": results, "warnings": warnings, "failures": failures}
    print(json.dumps(report, indent=2, default=str))
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
