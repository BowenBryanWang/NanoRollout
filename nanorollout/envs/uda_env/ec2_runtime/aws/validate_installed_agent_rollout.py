#!/usr/bin/env python3
"""Validate a completed UDA installed-agent rollout on EC2."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


AGENT_ARTIFACTS = {
    "claude-code": ("trajectory.json", "claude-code.txt"),
    "claude": ("trajectory.json", "claude-code.txt"),
    "codex": ("trajectory.json", "codex.jsonl", "last-message.txt"),
    "codex-cli": ("trajectory.json", "codex.jsonl", "last-message.txt"),
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("output_dir", help="Rollout output directory or parent directory.")
    p.add_argument("--region", default="ap-southeast-1")
    p.add_argument("--profile", default=None)
    p.add_argument("--expected-agent", default=None)
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


def validate_json_file(path: Path) -> tuple[dict[str, Any], Any | None, str | None]:
    payload, error = read_json(path)
    return status(payload is not None, str(path) if payload is not None else {"path": str(path), "error": error}), payload, error


def expected_artifacts(agent: str | None) -> tuple[str, ...]:
    if agent:
        return AGENT_ARTIFACTS.get(agent, ("trajectory.json",))
    return ("trajectory.json",)


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
        check, payload, _ = validate_json_file(path)
        checks[f"{name}_json"] = check
        if not check["ok"]:
            failures.append(f"{name}_json")
        else:
            payloads[name] = payload

    metadata = payloads.get("metadata") or {}
    runtime = metadata.get("sandbox_runtime") or {}
    runtime_type = runtime.get("runtime_type") or runtime.get("type")
    checks["runtime_type"] = status(runtime_type == "ec2", runtime_type)
    if runtime_type != "ec2":
        failures.append("runtime_type")

    agent = args.expected_agent or metadata.get("agent")
    if args.expected_agent:
        checks["expected_agent"] = status(metadata.get("agent") == args.expected_agent, metadata.get("agent"))
        if not checks["expected_agent"]["ok"]:
            failures.append("expected_agent")

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
    checks["trajectory_shape"] = status(bool(trajectory), sorted(trajectory) if isinstance(trajectory, dict) else type(trajectory).__name__)
    if not checks["trajectory_shape"]["ok"]:
        failures.append("trajectory_shape")

    agent_dir = trial_dir / "agent"
    artifacts = [agent_dir / name for name in expected_artifacts(agent)]
    existing = [str(path) for path in artifacts if path.exists() and path.stat().st_size > 0]
    checks["agent_artifacts"] = status(len(existing) == len(artifacts), existing)
    if not checks["agent_artifacts"]["ok"]:
        failures.append("agent_artifacts")

    if (agent_dir / "trajectory.json").exists():
        check, payload, _ = validate_json_file(agent_dir / "trajectory.json")
        checks["agent_trajectory_json"] = check
        if not check["ok"]:
            failures.append("agent_trajectory_json")
        elif isinstance(payload, dict):
            checks["agent_trajectory_shape"] = status(bool(payload), sorted(payload))

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
