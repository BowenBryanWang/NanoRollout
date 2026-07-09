#!/usr/bin/env python3
"""Validate a completed UDA Claude Code rollout on EC2."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("output_dir", help="Rollout output directory or parent directory.")
    p.add_argument("--region", default="ap-southeast-1")
    p.add_argument("--profile", default=None)
    p.add_argument(
        "--skip-aws-cleanup-check",
        action="store_true",
        help="Only validate local artifacts; do not query EC2 instance state.",
    )
    return p.parse_args()


def read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, str(exc)


def find_trial_dirs(root: Path) -> list[Path]:
    if (root / "metadata.json").is_file():
        return [root]
    trials = []
    for metadata in root.rglob("metadata.json"):
        if ".venv" in metadata.parts:
            continue
        trials.append(metadata.parent)
    return sorted(trials, key=lambda p: p.stat().st_mtime)


def status(ok: bool, detail: Any = None) -> dict[str, Any]:
    return {"ok": bool(ok), "detail": detail}


def validate_trial(trial_dir: Path, args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    checks: dict[str, Any] = {"trial_dir": str(trial_dir)}
    failures: list[str] = []

    required = {
        "trajectory": trial_dir / "trajectory.json",
        "reward": trial_dir / "reward.json",
        "metadata": trial_dir / "metadata.json",
    }
    payloads: dict[str, Any] = {}
    for name, path in required.items():
        payload, error = read_json(path)
        ok = payload is not None
        checks[f"{name}_json"] = status(ok, str(path) if ok else {"path": str(path), "error": error})
        if not ok:
            failures.append(f"{name}_json")
        else:
            payloads[name] = payload

    metadata = payloads.get("metadata") or {}
    runtime = metadata.get("sandbox_runtime") or {}
    runtime_type = runtime.get("runtime_type") or runtime.get("type")
    checks["runtime_type"] = status(runtime_type == "ec2", runtime_type)
    if runtime_type != "ec2":
        failures.append("runtime_type")

    instance_id = runtime.get("instance_id")
    checks["instance_id"] = status(isinstance(instance_id, str) and instance_id.startswith("i-"), instance_id)
    if not checks["instance_id"]["ok"]:
        failures.append("instance_id")

    reward = payloads.get("reward") or {}
    checks["reward_shape"] = status(
        "reward" in reward and "resolved" in reward,
        {"reward": reward.get("reward"), "resolved": reward.get("resolved")},
    )
    if not checks["reward_shape"]["ok"]:
        failures.append("reward_shape")

    trajectory = payloads.get("trajectory") or {}
    conversation = trajectory.get("conversation") or []
    checks["trajectory_shape"] = status(
        isinstance(conversation, list) and len(conversation) > 0,
        {"conversation_len": len(conversation) if isinstance(conversation, list) else None},
    )
    if not checks["trajectory_shape"]["ok"]:
        failures.append("trajectory_shape")

    agent_dir = trial_dir / "agent"
    agent_artifacts = [
        agent_dir / "trajectory.json",
        agent_dir / "claude-code.txt",
    ]
    existing_agent_artifacts = [str(path) for path in agent_artifacts if path.exists()]
    checks["agent_artifacts"] = status(bool(existing_agent_artifacts), existing_agent_artifacts)
    if not existing_agent_artifacts:
        failures.append("agent_artifacts")

    if not args.skip_aws_cleanup_check and isinstance(instance_id, str):
        try:
            import boto3

            session_kwargs: dict[str, str] = {"region_name": args.region}
            if args.profile:
                session_kwargs["profile_name"] = args.profile
            ec2 = boto3.Session(**session_kwargs).client("ec2")
            resp = ec2.describe_instances(InstanceIds=[instance_id])
            states = [
                inst.get("State", {}).get("Name")
                for res in resp.get("Reservations", [])
                for inst in res.get("Instances", [])
            ]
            ok = all(state in {"terminated", "shutting-down"} for state in states)
            checks["ec2_cleanup"] = status(ok, states)
            if not ok:
                failures.append("ec2_cleanup")
        except Exception as exc:
            checks["ec2_cleanup"] = status(False, str(exc))
            failures.append("ec2_cleanup")

    return checks, failures


def main() -> int:
    args = parse_args()
    root = Path(args.output_dir).expanduser().resolve()
    trials = find_trial_dirs(root)
    if not trials:
        print(json.dumps({"ok": False, "failures": ["no_trials"], "root": str(root)}, indent=2))
        return 1

    reports = []
    failures = []
    for trial in trials:
        report, trial_failures = validate_trial(trial, args)
        reports.append(report)
        if trial_failures:
            failures.append({"trial_dir": str(trial), "failures": trial_failures})

    print(json.dumps({"ok": not failures, "trials": reports, "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
