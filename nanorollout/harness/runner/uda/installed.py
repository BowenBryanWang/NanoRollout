"""UDA bench runner for Harbor-style installed CLI agents.

CLI-only baseline: spins up a uda-desktop sandbox (docker / modal),
runs the per-bench driver's ``setup_workspace`` + ``run_warmup`` to
stage the task workspace, then hands the container off to the
installed agent (Claude Code today; Qwen Code / OpenCode wired through
the same path). The agent runs entirely via ``bash`` / ``editor`` /
``read`` on the container filesystem — no GUI bridge yet. A
follow-up will add an MCP server exposing uda-desktop's
``/v1/computer-use/*`` to lift this restriction.

Mirrors :mod:`nanorollout.harness.runner.terminal.installed` so the
same agent registry, credential injection, and metadata shape apply
unchanged. Output layout matches :func:`run_uda_agent` so callers can
collate results across the Python-controller path and this CLI-agent
path uniformly.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from nanorollout.envs.uda_env.shell_adapter import UdaShellAdapter
from nanorollout.harness.agents.shared import ClaudeCode, CodexCli, OpenCode, QwenCode
from nanorollout.harness.runner.uda.uda_agent import (
    _allocate_port,
    _attach_trial_log,
    _build_reward_payload,
    _build_uda_config,
    _coerce_bool,
    _detect_encrypted_task,
    _ensure_logging,
    _load_task,
    _parse_sampling_params,
    _resolve_bench,
    _resolve_task_root,
    _write_json,
)


logger = logging.getLogger(__name__)


AGENT_REGISTRY = {
    "claude-code": ClaudeCode,
    "codex": CodexCli,
    "codex-cli": CodexCli,
    "qwen-code": QwenCode,
    "qwen-coder": QwenCode,
    "opencode": OpenCode,
}


PROVIDER_API_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GEMINI_API_KEY",
}


def _inject_agent_credentials(
    agent_name: str,
    model_name: str,
    api_key: Optional[str],
    base_url: Optional[str],
    extra_env: dict[str, str],
) -> None:
    if agent_name == "claude-code":
        if api_key:
            extra_env.setdefault("ANTHROPIC_API_KEY", api_key)
        if base_url:
            extra_env.setdefault("ANTHROPIC_BASE_URL", base_url)
        return
    if agent_name in ("qwen-code", "qwen-coder"):
        if api_key:
            extra_env.setdefault("OPENAI_API_KEY", api_key)
        if base_url:
            extra_env.setdefault("OPENAI_BASE_URL", base_url)
        return
    if agent_name == "opencode":
        provider = model_name.split("/", 1)[0] if "/" in model_name else "openai"
        env_key = PROVIDER_API_ENV.get(provider)
        if api_key and env_key:
            extra_env.setdefault(env_key, api_key)
        if base_url and provider == "openai":
            extra_env.setdefault("OPENAI_BASE_URL", base_url)


def _build_agent(
    agent_name: str,
    *,
    trial_dir: Path,
    model_name: str,
    api_key: Optional[str],
    base_url: Optional[str],
    extra_args: Dict[str, Any],
):
    if agent_name not in AGENT_REGISTRY:
        raise ValueError(
            f"Unsupported installed UDA agent: {agent_name!r}. "
            f"Known: {sorted(AGENT_REGISTRY)}."
        )
    agent_cls = AGENT_REGISTRY[agent_name]
    agent_kwargs = dict(extra_args.get("agent_kwargs") or {})

    extra_env = dict(extra_args.get("agent_env", {}) or {})
    extra_env.update(agent_kwargs.pop("extra_env", {}) or {})
    extra_env.update(agent_kwargs.pop("agent_env", {}) or {})
    _inject_agent_credentials(agent_name, model_name, api_key, base_url, extra_env)

    agent_kwargs["logs_dir"] = trial_dir / "agent"
    agent_kwargs["model_name"] = model_name
    agent_kwargs["extra_env"] = extra_env
    return agent_cls(**agent_kwargs)


def _build_synthetic_result(
    task: dict,
    config: dict,
    agent_result,
    workspace_dir: str,
    runtime_metadata: dict,
) -> dict:
    """Shape an installed-agent rollout as a uda-env ``run_task`` result.

    Cocoa-v1's ``test.py`` may inspect ``conversation`` / ``task_result``;
    wildclaw-v1's ``grade.py`` ignores the rollout result entirely and
    inspects container filesystem state, so this is best-effort: the
    fields exist with sane defaults, but Claude Code's free-form text
    output won't satisfy graders that expect a specific ``task_result``
    shape. Use the wildclaw / filesystem-based subset of the corpus when
    measuring this baseline.
    """
    history = agent_result.history if agent_result else []
    last_msg = ""
    for msg in reversed(history):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content") or ""
            if isinstance(content, str) and content.strip():
                last_msg = content.strip()
                break

    return {
        **task,
        "status": "success" if agent_result and not agent_result.error else "error",
        "iterations": agent_result.iterations if agent_result else 0,
        "conversation": history,
        "task_result": last_msg,
        "sandbox_runtime": runtime_metadata,
        "config": config,
    }


def _build_agent_metrics(agent_result, agent_time: float, eval_time: float) -> dict:
    history = agent_result.history if agent_result else []
    turns = sum(1 for m in history if isinstance(m, dict) and m.get("role") == "assistant")
    tool_calls = sum(
        len(m.get("tool_calls") or [])
        for m in history
        if isinstance(m, dict)
    )
    return {
        "turns": turns,
        "tool_calls": tool_calls,
        "model_query_time_sum": 0.0,
        "env_execution_time_sum": 0.0,
        "eval_time": eval_time,
        "agent_run_time": agent_time,
        "total_time": agent_time + eval_time,
    }


def _capture_pre_rollout_screenshot(sandbox_client: Any, output_root: Path) -> dict[str, Any]:
    """Save desktop/browser/app state after setup and before the installed agent acts."""
    screenshot_dir = output_root / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / "pre_rollout.png"
    metadata_path = screenshot_dir / "pre_rollout.json"
    info: dict[str, Any] = {
        "ok": False,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "path": str(screenshot_path.relative_to(output_root)),
    }

    def quality_issue(image_bytes: bytes) -> str | None:
        try:
            from io import BytesIO

            from PIL import Image, ImageStat

            with Image.open(BytesIO(image_bytes)) as image:
                rgb = image.convert("RGB")
                width, height = rgb.size
                stat = ImageStat.Stat(rgb)
                mean_luma = sum(stat.mean) / 3.0
                pixels = rgb.load()
                sampled = 0
                nonblack = 0
                step_x = max(1, width // 160)
                step_y = max(1, height // 90)
                for y in range(0, height, step_y):
                    for x in range(0, width, step_x):
                        sampled += 1
                        if max(pixels[x, y]) > 24:
                            nonblack += 1
                ratio = nonblack / sampled if sampled else 0.0
        except Exception as exc:
            return f"screenshot_quality_check_failed: {type(exc).__name__}: {exc}"
        if mean_luma < 3.0 or ratio < 0.005:
            return f"screenshot_nearly_black mean_luma={mean_luma:.2f} nonblack_ratio={ratio:.4f}"
        return None

    try:
        if sandbox_client is None or not hasattr(sandbox_client, "take_screenshot"):
            info["error"] = "sandbox_client_take_screenshot_unavailable"
            return info
        attempts = []
        last_error = "empty_screenshot_payload"
        for attempt in range(1, 7):
            screenshot_base64, status = sandbox_client.take_screenshot()
            attempt_info: dict[str, Any] = {"attempt": attempt, "status": status}
            if not screenshot_base64:
                attempt_info["error"] = "empty_screenshot_payload"
                attempts.append(attempt_info)
                last_error = "empty_screenshot_payload"
                time.sleep(2)
                continue
            if isinstance(screenshot_base64, str) and "," in screenshot_base64[:64]:
                screenshot_base64 = screenshot_base64.split(",", 1)[1]
            screenshot_bytes = base64.b64decode(screenshot_base64)
            attempt_info["bytes"] = len(screenshot_bytes)
            issue = quality_issue(screenshot_bytes)
            attempt_info["quality_issue"] = issue
            attempts.append(attempt_info)
            if len(screenshot_bytes) > 0 and issue is None:
                screenshot_path.write_bytes(screenshot_bytes)
                info["bytes"] = len(screenshot_bytes)
                info["status"] = status
                info["attempts"] = attempts
                info["ok"] = True
                return info
            last_error = issue or "empty_screenshot_file"
            time.sleep(2)
        info["attempts"] = attempts
        info["error"] = last_error
    except Exception as exc:
        logger.exception("Failed to capture pre-rollout screenshot")
        info["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        metadata_path.write_text(json.dumps(info, indent=2), encoding="utf-8")

    return info


def _run_installed_uda_agent(
    agent_name: str,
    instance_id: str,
    output_dir: str,
    model_name: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    env_type: str = "docker",
    sampling_params: Optional[object] = None,
    extra_args: Dict[str, Any] = {},
    *,
    bench: Optional[str] = None,
) -> Dict[str, Any]:
    extra_args = dict(extra_args or {})
    resolved_bench = _resolve_bench(bench, extra_args)
    extra_args.setdefault("bench", resolved_bench)

    log_level_name = str(extra_args.get("log_level", "INFO")).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    _ensure_logging(log_level)
    started = time.time()
    env_type = env_type or "docker"
    sampling_params_dict = _parse_sampling_params(sampling_params)

    output_root = Path(output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    trial_log_path = output_root / "trial.log"

    result: Dict[str, Any] = {}
    agent_result = None
    eval_result: Optional[Dict[str, Any]] = None
    error_msg: Optional[str] = None
    agent_time = 0.0
    eval_time = 0.0
    sandbox_client = None
    runtime_metadata: Dict[str, Any] = {}
    pre_rollout_screenshot: Dict[str, Any] = {}

    with _attach_trial_log(trial_log_path, log_level):
        logger.info(
            "[%s] Installed agent %s on uda-env (bench=%s)",
            instance_id, agent_name, resolved_bench,
        )
        try:
            tasks_dir, task_dir = _resolve_task_root(
                instance_id, extra_args, bench=resolved_bench
            )
            encrypted_task = _detect_encrypted_task(task_dir)
            preferred_port = extra_args.get("docker_port")
            docker_port = _allocate_port(
                int(preferred_port) if preferred_port is not None else None
            )

            config = _build_uda_config(
                model_name=model_name,
                base_url=base_url,
                api_key=api_key,
                env_type=env_type,
                sampling_params=sampling_params_dict,
                extra_args=extra_args,
                encrypted_task=encrypted_task,
                docker_port=docker_port,
            )
            sandbox_config = config.setdefault("sandbox", {})
            sandbox_config.setdefault("bench", resolved_bench)
            for key in ("uda_image", "corpus_revision"):
                if key in extra_args:
                    sandbox_config.setdefault(key, extra_args[key])

            _write_json(output_root / "uda_config.json", config)

            from nanorollout.envs.uda_env import (
                UnifiedSandboxClient,
                setup_logging,
            )
            from nanorollout.envs.uda_env.driver import load_driver

            setup_logging(
                str(config.get("log_level", log_level_name)),
                log_file=str(trial_log_path),
            )

            task = _load_task(
                task_dir,
                _coerce_bool(config.get("use_encrypted_tasks"), default=encrypted_task),
            )
            from nanorollout.envs.uda_env.runtime_profile import (
                apply_task_runtime_to_sandbox_config,
            )

            apply_task_runtime_to_sandbox_config(task, sandbox_config)
            _write_json(output_root / "uda_config.json", config)

            # Always use UnifiedSandboxClient — installed agents need shell
            # + filesystem; computer-use endpoints are unused on this path.
            sandbox_client = UnifiedSandboxClient(sandbox_config=sandbox_config)
            wait_time = int(
                extra_args.get("create_timeout")
                or extra_args.get("env_timeout")
                or 30
            )

            logger.info(
                "[%s] Creating uda-env sandbox (runtime=%s)",
                instance_id, sandbox_config.get("runtime_type", "docker"),
            )
            if not sandbox_client.create_environment(task, wait_time):
                raise RuntimeError("uda-env sandbox failed to start")
            runtime_metadata = sandbox_client.get_runtime_metadata()

            driver_name = task.get("driver") or resolved_bench
            driver = load_driver(driver_name)
            driver.setup_workspace(sandbox_client.runtime, task)
            driver.run_warmup(sandbox_client.runtime, task)
            pre_rollout_screenshot = _capture_pre_rollout_screenshot(
                sandbox_client,
                output_root,
            )

            shell_env = UdaShellAdapter(
                sandbox_client.runtime,
                workspace_dir=(
                    runtime_metadata.get("workspace_dir")
                    or sandbox_config.get("workspace_dir")
                    or sandbox_config.get("ec2_workspace_dir")
                    or "/home/kasm-user"
                ),
                timeout=int(extra_args.get("step_timeout") or 600),
            )
            shell_env.set_tool_log_context(instance_id)

            agent = _build_agent(
                agent_name,
                trial_dir=output_root,
                model_name=model_name,
                api_key=api_key,
                base_url=base_url,
                extra_args=extra_args,
            )
            instruction = task.get("instruction") or str(task)
            agent_timeout = extra_args.get("agent_timeout") or extra_args.get("step_timeout")

            logger.info("[%s] Running installed agent %s", instance_id, agent_name)
            agent_start = time.time()
            agent_result = agent.run(
                instruction,
                shell_env,
                timeout_sec=int(agent_timeout) if agent_timeout else None,
            )
            agent_time = time.time() - agent_start

            result = _build_synthetic_result(
                task, config, agent_result,
                workspace_dir=shell_env.workspace_dir,
                runtime_metadata=runtime_metadata,
            )

            logger.info("[%s] Running %s driver score()", instance_id, driver_name)
            eval_start = time.time()
            try:
                eval_payload = driver.score(sandbox_client.runtime, task, result)
            except Exception:
                logger.exception("driver.score raised for %s", instance_id)
                eval_payload = None
            eval_time = time.time() - eval_start
            if eval_payload is not None:
                eval_result = eval_payload
                result["eval"] = eval_payload
        except Exception as exc:
            error_msg = str(exc)
            logger.exception("Installed UDA agent run failed for %s", instance_id)
        finally:
            if sandbox_client is not None:
                try:
                    sandbox_client.cleanup_environment()
                except Exception:
                    logger.exception("uda-env sandbox cleanup failed for %s", instance_id)

    reward_payload = _build_reward_payload(instance_id, result, error_msg)
    metadata = {
        "instance_id": instance_id,
        "bench": resolved_bench,
        "agent": agent_name,
        "wall_time_sec": round(time.time() - started, 2),
        "sandbox_runtime": runtime_metadata or result.get("sandbox_runtime"),
        "pre_rollout_screenshot": pre_rollout_screenshot,
        "reward_payload": reward_payload,
    }
    if error_msg:
        metadata["error"] = error_msg

    if result:
        _write_json(output_root / "trajectory.json", result)
    _write_json(output_root / "reward.json", reward_payload)
    _write_json(output_root / "metadata.json", metadata)
    (output_root / "result.txt").write_text(
        f"{reward_payload['reward']}\n", encoding="utf-8"
    )

    messages = agent_result.history if agent_result else []
    exit_status = (
        "Error"
        if error_msg
        else ("Resolved" if reward_payload["resolved"] else "Completed")
    )
    response: Dict[str, Any] = {
        "reward": reward_payload["reward"],
        "messages": messages,
        "exit_status": exit_status,
        "agent_metrics": _build_agent_metrics(agent_result, agent_time, eval_time),
        "metadata": metadata,
        "tools": None,
    }
    if error_msg:
        response["error"] = error_msg
    return response


def run_uda_claude_code(
    instance_id: str,
    output_dir: str,
    model_name: str,
    base_url: str = None,
    api_key: str = None,
    env_type: str = "docker",
    sampling_params: Optional[object] = None,
    extra_args: Dict[str, Any] = {},
    *,
    bench: Optional[str] = None,
) -> Dict[str, Any]:
    """Run Claude Code (CLI-only baseline) on a single UDA task."""
    return _run_installed_uda_agent(
        "claude-code",
        instance_id,
        output_dir,
        model_name,
        base_url=base_url,
        api_key=api_key,
        env_type=env_type,
        sampling_params=sampling_params,
        extra_args=extra_args,
        bench=bench,
    )


def run_uda_qwen_code(
    instance_id: str,
    output_dir: str,
    model_name: str,
    base_url: str = None,
    api_key: str = None,
    env_type: str = "docker",
    sampling_params: Optional[object] = None,
    extra_args: Dict[str, Any] = {},
    *,
    bench: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_installed_uda_agent(
        "qwen-code",
        instance_id,
        output_dir,
        model_name,
        base_url=base_url,
        api_key=api_key,
        env_type=env_type,
        sampling_params=sampling_params,
        extra_args=extra_args,
        bench=bench,
    )


def run_uda_codex(
    instance_id: str,
    output_dir: str,
    model_name: str,
    base_url: str = None,
    api_key: str = None,
    env_type: str = "docker",
    sampling_params: Optional[object] = None,
    extra_args: Dict[str, Any] = {},
    *,
    bench: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_installed_uda_agent(
        "codex",
        instance_id,
        output_dir,
        model_name,
        base_url=base_url,
        api_key=api_key,
        env_type=env_type,
        sampling_params=sampling_params,
        extra_args=extra_args,
        bench=bench,
    )


def run_uda_opencode(
    instance_id: str,
    output_dir: str,
    model_name: str,
    base_url: str = None,
    api_key: str = None,
    env_type: str = "docker",
    sampling_params: Optional[object] = None,
    extra_args: Dict[str, Any] = {},
    *,
    bench: Optional[str] = None,
) -> Dict[str, Any]:
    return _run_installed_uda_agent(
        "opencode",
        instance_id,
        output_dir,
        model_name,
        base_url=base_url,
        api_key=api_key,
        env_type=env_type,
        sampling_params=sampling_params,
        extra_args=extra_args,
        bench=bench,
    )
