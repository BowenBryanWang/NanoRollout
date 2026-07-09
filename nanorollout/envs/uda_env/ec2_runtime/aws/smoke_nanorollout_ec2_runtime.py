#!/usr/bin/env python3
"""Launch a NanoRollout UDA EC2 runtime and verify SDK surfaces.

This is the integration smoke for the EC2 backend: it exercises
``UnifiedSandboxClient(runtime_type="ec2")`` end-to-end, then terminates
the instance through the runtime cleanup path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


DEFAULT_REGION = "ap-southeast-1"
DEFAULT_LAUNCH_TEMPLATE_ID = "lt-03862713037af59fe"
DEFAULT_SUBNET_ID = "subnet-0c85c17f888605401"
DEFAULT_SECURITY_GROUP_IDS = "sg-029a65325aae8f739"
DEFAULT_INSTANCE_PROFILE = "uda-gym-ec2-instance-profile"
DEFAULT_INSTANCE_TYPE = "t3.xlarge"
DEFAULT_WORKSPACE = "/home/user"


def bypass_proxy_env() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--nanorollout-root",
        default=".",
        help="Path to the NanoRollout repository root.",
    )
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--launch-template-id", default=DEFAULT_LAUNCH_TEMPLATE_ID)
    p.add_argument("--ami-id", default=None)
    p.add_argument("--instance-type", default=DEFAULT_INSTANCE_TYPE)
    p.add_argument("--subnet-id", default=DEFAULT_SUBNET_ID)
    p.add_argument("--security-group-ids", default=DEFAULT_SECURITY_GROUP_IDS)
    p.add_argument("--iam-instance-profile", default=DEFAULT_INSTANCE_PROFILE)
    p.add_argument("--workspace-dir", default=DEFAULT_WORKSPACE)
    p.add_argument("--health-timeout", type=int, default=900)
    p.add_argument("--instance-running-timeout", type=int, default=None)
    p.add_argument("--sdk-timeout", type=int, default=900)
    p.add_argument(
        "--check-claude-code-install",
        action="store_true",
        help="Install Claude Code inside the EC2 sandbox and verify claude --version.",
    )
    p.add_argument("--claude-install-timeout", type=int, default=600)
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
        "ec2_instance_running_timeout": args.instance_running_timeout
        or args.health_timeout,
        "ec2_wait_for_termination": True,
        "ec2_terminate_on_cleanup": not args.keep_instance,
        "sdk_timeout": args.sdk_timeout,
    }
    if not args.ami_id:
        sandbox_config["ec2_launch_template_id"] = args.launch_template_id

    client = UnifiedSandboxClient(sandbox_config=sandbox_config)
    task = {"task_name": "ec2-runtime-smoke", "task_id": "ec2-runtime-smoke"}
    created = False
    result: dict[str, object] = {"checks": {}}
    failures: list[str] = []

    try:
        created = client.create_environment(task, wait_time=args.health_timeout)
        result["created"] = created
        result["metadata"] = client.get_runtime_metadata()
        if not created:
            failures.append("create_environment")
            return 1

        runtime = client.runtime

        exec_result = runtime.exec_in_runtime(
            "pwd && echo nano-ec2-smoke",
            workdir=args.workspace_dir,
            timeout=60,
        )
        result["checks"]["exec"] = exec_result
        if exec_result.get("returncode") != 0 or "nano-ec2-smoke" not in (
            exec_result.get("output") or ""
        ):
            failures.append("exec")

        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("nano ec2 copy\n")
            local_path = Path(fh.name)
        try:
            copy_ok = runtime.copy_to_runtime(str(local_path), "/tmp/nano-ec2-smoke.txt")
        finally:
            local_path.unlink(missing_ok=True)
        result["checks"]["copy"] = copy_ok
        if not copy_ok:
            failures.append("copy")

        cat_result = runtime.exec_in_runtime(
            "cat /tmp/nano-ec2-smoke.txt",
            workdir=args.workspace_dir,
            timeout=60,
        )
        result["checks"]["file_roundtrip"] = cat_result
        if "nano ec2 copy" not in (cat_result.get("output") or ""):
            failures.append("file_roundtrip")

        tmp_workspace_result = runtime.exec_in_runtime(
            (
                "set -e; "
                "test -w /tmp_workspace; "
                "mkdir -p /tmp_workspace/results; "
                "test -w /tmp_workspace/results; "
                "echo smoke >/tmp_workspace/results/uda-ec2-smoke.txt; "
                "cat /tmp_workspace/results/uda-ec2-smoke.txt"
            ),
            workdir=args.workspace_dir,
            timeout=60,
        )
        result["checks"]["tmp_workspace"] = tmp_workspace_result
        if "smoke" not in (tmp_workspace_result.get("output") or ""):
            failures.append("tmp_workspace")

        screenshot = client.get_feedback({"action_type": "computer_use_screenshot"})
        result["checks"]["screenshot"] = {
            "done": screenshot.get("done"),
            "message": screenshot.get("message"),
            "base64_bytes": len(screenshot.get("image_base64") or ""),
        }
        if not screenshot.get("image_base64"):
            failures.append("screenshot")

        if args.check_claude_code_install:
            from nanorollout.envs.uda_env.shell_adapter import UdaShellAdapter
            from nanorollout.harness.agents.shared import ClaudeCode

            with tempfile.TemporaryDirectory(prefix="uda-ec2-claude-smoke-") as tmp:
                shell_env = UdaShellAdapter(
                    runtime,
                    workspace_dir=args.workspace_dir,
                    timeout=args.claude_install_timeout,
                )
                agent = ClaudeCode(
                    logs_dir=Path(tmp) / "agent",
                    model_name="claude-sonnet-4-6",
                    install_timeout_sec=args.claude_install_timeout,
                )
                try:
                    agent.install(shell_env)
                    version_result = shell_env.execute(
                        'export PATH="$HOME/.local/bin:$PATH"; claude --version',
                        timeout=120,
                    )
                except Exception as exc:
                    result["checks"]["claude_code_install"] = {
                        "ok": False,
                        "error": str(exc),
                    }
                    failures.append("claude_code_install")
                else:
                    output = version_result.output.strip()
                    ok = version_result.exit_code == 0 and "claude" in output.lower()
                    result["checks"]["claude_code_install"] = {
                        "ok": ok,
                        "exit_code": version_result.exit_code,
                        "output": output,
                    }
                    if not ok:
                        failures.append("claude_code_install")
    finally:
        if created and not args.keep_instance:
            result["cleanup"] = client.cleanup_environment()

    result["failures"] = failures
    print(json.dumps(result, indent=2, default=str)[:12000])
    if failures:
        print(f"FAILED: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("NanoRollout EC2 runtime smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
