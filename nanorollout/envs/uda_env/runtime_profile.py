"""Task-level runtime/profile routing for native UDA-Gym tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

import yaml


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _lower_set(values: Iterable[Any]) -> set[str]:
    return {_clean(value).lower() for value in values if _clean(value)}


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "nanorollout").is_dir():
            return parent
    return here.parents[3]


def _default_profiles_path() -> Path:
    packaged_path = Path(__file__).resolve().parent / "ec2_runtime" / "env_profiles.yaml"
    if packaged_path.exists():
        return packaged_path
    return _repo_root() / "nanorollout" / "envs" / "uda_env" / "ec2_runtime" / "env_profiles.yaml"


def _load_profiles(path: str | None = None) -> dict[str, dict[str, Any]]:
    profile_path = Path(path).expanduser().resolve() if path else _default_profiles_path()
    if not profile_path.exists():
        return {}
    loaded = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    profiles = loaded.get("profiles") if isinstance(loaded, dict) else {}
    return profiles if isinstance(profiles, dict) else {}


def _resolve_alias(profile: str, profiles: Mapping[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    name = profile
    seen: set[str] = set()
    while name in profiles:
        data = profiles[name] or {}
        alias = data.get("alias_for")
        if not alias:
            return name, dict(data)
        if name in seen:
            raise ValueError(f"runtime profile alias loop at {profile!r}")
        seen.add(name)
        name = str(alias)
    raise ValueError(f"runtime profile {profile!r} is not defined in env_profiles.yaml")


def _normalize_runtime(runtime: Any) -> dict[str, Any]:
    if not isinstance(runtime, dict):
        return {}
    out = dict(runtime)
    env = runtime.get("env")
    if isinstance(env, dict):
        if "env_type" not in out and "type" in env:
            out["env_type"] = env.get("type")
        if "env_profile" not in out and "profile" in env:
            out["env_profile"] = env.get("profile")
        for key in ("region", "instance_type", "workspace_dir", "disk_gb_min"):
            out.setdefault(key, env.get(key))
    if "env_profile" not in out:
        out["env_profile"] = runtime.get("profile")
    if "env_type" not in out:
        out["env_type"] = runtime.get("type")
    requires = runtime.get("requires")
    if isinstance(requires, dict):
        out.setdefault("required_software", requires.get("software"))
        out.setdefault("required_services", requires.get("services"))
        out.setdefault("requires_gui", requires.get("gui"))
        out.setdefault("requires_browser", requires.get("browser"))
    fallback = runtime.get("fallback")
    if isinstance(fallback, dict):
        if "allow_profile_fallback" not in out:
            out["allow_profile_fallback"] = fallback.get("allow_general_root")
    return {k: v for k, v in out.items() if v is not None}


def _profile_includes(profile_data: Mapping[str, Any]) -> str:
    includes = profile_data.get("includes") or []
    services = profile_data.get("services") or []
    return "\n".join(str(item).lower() for item in [*includes, *services])


def _missing_software(
    required: Iterable[Any],
    profile_data: Mapping[str, Any],
) -> list[str]:
    haystack = _profile_includes(profile_data)
    missing: list[str] = []
    for item in sorted(_lower_set(required)):
        if item not in haystack:
            missing.append(item)
    return missing


def task_runtime(task: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized task runtime declaration, if present."""
    runtime = task.get("runtime")
    if isinstance(runtime, dict):
        return _normalize_runtime(runtime)
    return {}


def apply_task_runtime_to_sandbox_config(
    task: Mapping[str, Any],
    sandbox_config: Dict[str, Any],
) -> dict[str, Any]:
    """Merge task runtime/profile requirements into a mutable sandbox config.

    Explicit CLI/sandbox values stay authoritative where possible. For EC2
    profile tasks, the profile AMI is filled from env_profiles.yaml and
    conflicting launch-template fields are cleared unless the caller opts into
    launch-template override with ``ec2_allow_launch_template_override``.
    """
    runtime = task_runtime(task)
    if runtime:
        sandbox_config.setdefault("task_runtime", runtime)

    required_env_type = _clean(runtime.get("env_type")).lower()
    current_runtime_type = _clean(sandbox_config.get("runtime_type")).lower()
    if required_env_type:
        if current_runtime_type and current_runtime_type != required_env_type:
            allow = bool(runtime.get("allow_env_type_override", False))
            if not allow:
                raise ValueError(
                    f"task requires env_type={required_env_type!r}, "
                    f"but runner is configured for runtime_type={current_runtime_type!r}"
                )
        else:
            sandbox_config.setdefault("runtime_type", required_env_type)

    profile = (
        _clean(runtime.get("env_profile"))
        or _clean(sandbox_config.get("ec2_env_profile"))
        or _clean(sandbox_config.get("env_profile"))
    )
    if not profile:
        return sandbox_config

    profiles = _load_profiles(_clean(sandbox_config.get("ec2_env_profiles_path")) or None)
    resolved_name, profile_data = _resolve_alias(profile, profiles)
    status = _clean(profile_data.get("status")).lower()
    explicit_ami_id = _clean(sandbox_config.get("ec2_ami_id") or sandbox_config.get("ami_id"))
    if status and "validated" not in status and not explicit_ami_id:
        raise ValueError(f"runtime profile {profile!r} is not rollout-ready (status={status!r})")

    required_software = runtime.get("required_software") or []
    allow_fallback = bool(runtime.get("allow_profile_fallback", False))
    missing = _missing_software(required_software, profile_data)
    if missing and not allow_fallback:
        raise ValueError(
            f"runtime profile {resolved_name!r} is missing required software: {', '.join(missing)}"
        )

    sandbox_config["ec2_env_profile"] = resolved_name
    sandbox_config["env_profile"] = resolved_name
    sandbox_config.setdefault("ec2_region", profile_data.get("region"))
    sandbox_config.setdefault("ec2_workspace_dir", profile_data.get("workspace_dir"))
    sandbox_config.setdefault("ec2_instance_type", runtime.get("instance_type") or profile_data.get("default_instance_type"))

    ami_id = _clean(profile_data.get("ami_id"))
    use_lt_override = bool(sandbox_config.get("ec2_allow_launch_template_override", False))
    profile_has_task_requirement = bool(runtime.get("env_profile"))
    if ami_id and (profile_has_task_requirement or not sandbox_config.get("ec2_ami_id")):
        sandbox_config["ec2_ami_id"] = ami_id
    selected_ami_id = _clean(sandbox_config.get("ec2_ami_id") or sandbox_config.get("ami_id"))
    if selected_ami_id and not use_lt_override and resolved_name not in {"general-root", "general"}:
        for key in (
            "ec2_launch_template_id",
            "ec2_launch_template_name",
            "ec2_launch_template_version",
        ):
            sandbox_config.pop(key, None)

    sandbox_config.setdefault("required_software", list(_lower_set(required_software)))
    return sandbox_config
