#!/usr/bin/env python3
"""Launch a UDA profile AMI and verify its profile marker/tooling."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


DEFAULT_REGION = "ap-southeast-1"
DEFAULT_SUBNET_ID = "subnet-0c85c17f888605401"
DEFAULT_SECURITY_GROUP_IDS = "sg-029a65325aae8f739"
DEFAULT_INSTANCE_PROFILE = "uda-gym-ec2-instance-profile"
DEFAULT_INSTANCE_TYPE = "t3.xlarge"
DEFAULT_WORKSPACE = "/home/user"


PROFILE_COMMANDS = {
    "multimedia": [
        "blender",
        "kdenlive",
        "openshot-qt",
        "audacity",
        "HandBrakeCLI",
        "vlc",
        "obs",
        "shotcut",
        "melt",
        "ffmpeg",
        "mediainfo",
    ],
    "multimedia-blender5": [
        "blender",
        "kdenlive",
        "openshot-qt",
        "audacity",
        "HandBrakeCLI",
        "vlc",
        "obs",
        "shotcut",
        "melt",
        "ffmpeg",
        "mediainfo",
    ],
    "datascience": [
        "grafana-server",
        "metabase",
        "uda-jupyter-lab",
        "uda-streamlit",
        "uda-superset",
        "psql",
        "sqlite3",
        "R",
        "dot",
        "java",
    ],
}


def bypass_proxy_env() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--nanorollout-root", default=".")
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--ami-id", required=True)
    p.add_argument("--profile-name", required=True)
    p.add_argument("--instance-type", default=DEFAULT_INSTANCE_TYPE)
    p.add_argument("--subnet-id", default=DEFAULT_SUBNET_ID)
    p.add_argument("--security-group-ids", default=DEFAULT_SECURITY_GROUP_IDS)
    p.add_argument("--iam-instance-profile", default=DEFAULT_INSTANCE_PROFILE)
    p.add_argument("--workspace-dir", default=DEFAULT_WORKSPACE)
    p.add_argument("--health-timeout", type=int, default=900)
    p.add_argument("--sdk-timeout", type=int, default=900)
    p.add_argument("--keep-instance", action="store_true")
    return p.parse_args()


def load_nanorollout(root: str) -> None:
    repo_root = Path(root).expanduser().resolve()
    if not (repo_root / "nanorollout").is_dir():
        raise SystemExit(f"NanoRollout root not found: {repo_root}")
    sys.path.insert(0, str(repo_root))


def main() -> int:
    bypass_proxy_env()
    args = parse_args()
    load_nanorollout(args.nanorollout_root)

    from nanorollout.envs.uda_env import UnifiedSandboxClient

    sandbox_config = {
        "runtime_type": "ec2",
        "client_type": "unified",
        "bench": "wildclaw-v1",
        "ec2_region": args.region,
        "ec2_ami_id": args.ami_id,
        "ec2_instance_type": args.instance_type,
        "ec2_subnet_id": args.subnet_id,
        "ec2_security_group_ids": args.security_group_ids,
        "ec2_iam_instance_profile": args.iam_instance_profile,
        "ec2_workspace_dir": args.workspace_dir,
        "ec2_health_timeout": args.health_timeout,
        "ec2_instance_running_timeout": args.health_timeout,
        "ec2_wait_for_termination": True,
        "ec2_terminate_on_cleanup": not args.keep_instance,
        "sdk_timeout": args.sdk_timeout,
    }

    client = UnifiedSandboxClient(sandbox_config=sandbox_config)
    task = {"task_name": f"ec2-profile-{args.profile_name}-smoke", "task_id": f"profile-{args.profile_name}"}
    created = False
    result: dict[str, object] = {"checks": {}, "failures": []}
    failures: list[str] = []

    try:
        created = client.create_environment(task, wait_time=args.health_timeout)
        result["created"] = created
        result["metadata"] = client.get_runtime_metadata()
        if not created:
            failures.append("create_environment")
            return 1

        runtime = client.runtime
        marker = f"/opt/uda-ec2/profile-{args.profile_name}.ready"
        manifest = f"/opt/uda-ec2/profile-{args.profile_name}.json"
        commands = PROFILE_COMMANDS.get(args.profile_name, [])
        command_check = " ".join(
            f"command -v {cmd} >/dev/null && echo CMD_OK:{cmd} || echo CMD_MISSING:{cmd};"
            for cmd in commands
        )
        shell = runtime.exec_in_runtime(
            (
                "set -e; "
                f"test -f {marker}; "
                f"python3 -m json.tool {manifest}; "
                f"{command_check} "
                "echo __UDA_PROFILE_SMOKE_OK__"
            ),
            workdir=args.workspace_dir,
            timeout=120,
        )
        output = shell.get("output") or ""
        result["checks"]["profile_marker_and_commands"] = shell
        if "__UDA_PROFILE_SMOKE_OK__" not in output:
            failures.append("profile_marker")
        missing = [line.split(":", 1)[1] for line in output.splitlines() if line.startswith("CMD_MISSING:")]
        result["checks"]["missing_commands"] = missing
        if missing:
            failures.append("commands")

        if args.profile_name == "multimedia-blender5":
            blender_shell = runtime.exec_in_runtime(
                (
                    "set -u; "
                    "blender --version | head -n 1; "
                    "blender --version | head -n 1 | grep -F 'Blender 5.1.2'; "
                    "if command -v blender3 >/dev/null 2>&1; then "
                    "  blender3 --version | head -n 1; "
                    "fi; "
                    "test \"$(readlink -f /usr/local/bin/blender)\" = "
                    "\"/opt/blender/blender-5.1.2-linux-x64/blender\"; "
                    "echo __UDA_BLENDER5_OK__"
                ),
                workdir=args.workspace_dir,
                timeout=60,
            )
            result["checks"]["blender5"] = blender_shell
            if "__UDA_BLENDER5_OK__" not in (blender_shell.get("output") or ""):
                failures.append("blender5")

        screenshot = client.get_feedback({"action_type": "computer_use_screenshot"})
        result["checks"]["screenshot"] = {
            "done": screenshot.get("done"),
            "message": screenshot.get("message"),
            "base64_bytes": len(screenshot.get("image_base64") or ""),
        }
        if not screenshot.get("image_base64"):
            failures.append("screenshot")

        if args.profile_name == "datascience":
            service_shell = runtime.exec_in_runtime(
                r"""
set -u
ok=0
for i in $(seq 1 36); do
  echo ITER:$i
  for svc in grafana-server metabase uda-jupyter postgresql; do
    echo SERVICE:$svc:$(systemctl is-active $svc):$(systemctl is-enabled $svc)
  done
  echo LISTEN_BEGIN
  ss -ltn | awk 'NR==1 || /:(3000|3001|8888|5432)( |$)/'
  echo LISTEN_END
  grafana=$(curl -fsS --max-time 5 http://127.0.0.1:3000/api/health 2>/dev/null || true)
  metabase=$(curl -fsS --max-time 5 http://127.0.0.1:3001/api/health 2>/dev/null || true)
  jupyter=$(curl -fsS --max-time 5 http://127.0.0.1:8888/api 2>/dev/null || true)
  echo GRAFANA_HEALTH:${grafana:-missing}
  echo METABASE_HEALTH:${metabase:-missing}
  echo JUPYTER_API:${jupyter:-missing}
  systemctl is-active --quiet grafana-server \
    && systemctl is-active --quiet metabase \
    && systemctl is-active --quiet uda-jupyter \
    && systemctl is-active --quiet postgresql \
    && [ -n "$grafana" ] \
    && [ -n "$metabase" ] \
    && [ -n "$jupyter" ] \
    && { ok=1; break; }
  sleep 10
done
[ "$ok" = 1 ] && echo __UDA_DATASCIENCE_SERVICES_OK__ || echo __UDA_DATASCIENCE_SERVICES_NOT_READY__
""",
                workdir=args.workspace_dir,
                timeout=430,
            )
            service_output = service_shell.get("output") or ""
            result["checks"]["datascience_services"] = service_shell
            if "__UDA_DATASCIENCE_SERVICES_OK__" not in service_output:
                failures.append("datascience_services")
    finally:
        if created and not args.keep_instance:
            result["cleanup"] = client.cleanup_environment()

    result["failures"] = failures
    print(json.dumps(result, indent=2, default=str)[:16000])
    if failures:
        print(f"FAILED: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"UDA EC2 profile smoke passed: {args.profile_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
