"""uda-gym driver.

Native UDA-Gym task contract:

- ``task.yaml`` or ``instruction.md`` supplies the agent-visible instruction.
- ``exec/`` is copied into ``/tmp_workspace/`` before the rollout.
- ``setup.sh`` runs inside the environment after ``exec/`` staging and before
  the agent starts. It is hidden harness code, suitable for CUA-Gym Hub state
  injection and opening Chrome to the hardened one-time launch URL.
- ``harness_env.tsv`` names host environment variables passed only to
  ``setup.sh`` and ``check.sh``. They are never staged into the agent-visible
  workspace.
- The driver also injects a randomized per-rollout
  ``UDA_GYM_HARNESS_STATE_DIR`` into ``setup.sh`` and ``check.sh`` only. Mock
  website setup should write session metadata there; the agent never receives
  this path.
- ``gt/`` is copied into ``/tmp_workspace/gt/`` only after the agent finishes.
- ``check.sh`` runs inside the environment for evaluation and must print one
  JSON object containing per-criterion scores, including ``overall_score`` when
  available.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import yaml

from ..logger import get_logger
from .base import discover_workspace_assets

if TYPE_CHECKING:
    from ..base import BaseSandboxRuntime

logger = get_logger("uda.driver.uda_gym")


class UdaGymDriver:
    """Driver for native UDA-Gym tasks."""

    name = "uda-gym"
    container_workspace = "/tmp_workspace"

    def load_task(self, task_dir: Path) -> Dict[str, Any]:
        assets = discover_workspace_assets(task_dir)

        task_data: Dict[str, Any] = {}
        if "task_yaml" in assets:
            with assets["task_yaml"].open(encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
            if not isinstance(loaded, dict):
                raise ValueError(f"uda-gym: task.yaml in {task_dir} must be a mapping")
            task_data = loaded

        instruction = task_data.get("instruction")
        instruction_path = task_dir / "instruction.md"
        if instruction and instruction_path.is_file():
            lowered = str(instruction).strip().lower()
            if (
                lowered.startswith("see instruction.md")
                or lowered.startswith("see query.md")
                or "agent-visible task" in lowered
            ):
                instruction = instruction_path.read_text(encoding="utf-8").strip()
        if not instruction and instruction_path.is_file():
            instruction = instruction_path.read_text(encoding="utf-8").strip()
        if not instruction:
            raise FileNotFoundError(
                f"uda-gym: {task_dir} must provide task.yaml:instruction or instruction.md"
            )

        meta: Dict[str, Any] = {}
        if "meta" in assets:
            with assets["meta"].open(encoding="utf-8") as fh:
                meta = json.load(fh)

        runtime: Dict[str, Any] = {}
        if isinstance(task_data.get("runtime"), dict):
            runtime.update(task_data["runtime"])
        if isinstance(meta.get("runtime"), dict):
            runtime.update(meta["runtime"])
        if "runtime_yaml" in assets:
            with assets["runtime_yaml"].open(encoding="utf-8") as fh:
                loaded_runtime = yaml.safe_load(fh)
            if isinstance(loaded_runtime, dict):
                runtime.update(loaded_runtime.get("runtime") or loaded_runtime)

        task = dict(task_data)
        task["driver"] = self.name
        task["task_dir"] = str(task_dir)
        task["task_name"] = task_dir.name
        task["task_id"] = meta.get("id", task_data.get("id", task_dir.name))
        task["category"] = meta.get("category", task_data.get("category", ""))
        task["instruction"] = str(instruction)
        task["timeout_seconds"] = meta.get("timeout_seconds") or task_data.get(
            "timeout_seconds", 600
        )
        task["exec_dir"] = str(assets["exec"]) if "exec" in assets else None
        task["hidden_dir"] = str(assets["hidden"]) if "hidden" in assets else None
        task["gt_dir"] = str(assets["gt"]) if "gt" in assets else None
        task["setup_path"] = str(assets["setup_sh"]) if "setup_sh" in assets else None
        task["check_path"] = str(assets["check_sh"]) if "check_sh" in assets else None
        task["env_tsv_path"] = str(assets["env_tsv"]) if "env_tsv" in assets else None
        task["harness_env_tsv_path"] = (
            str(assets["harness_env_tsv"]) if "harness_env_tsv" in assets else None
        )
        if runtime:
            task["runtime"] = runtime
        return task

    def get_extra_env(self, task: Dict[str, Any]) -> Dict[str, str]:
        return self._read_env_tsv(task.get("env_tsv_path"))

    def get_harness_env(self, task: Dict[str, Any]) -> Dict[str, str]:
        """Env vars for hidden setup/check scripts only.

        Unlike ``env.tsv`` these variables are not written to profile.d or the
        workspace, so secrets such as CUA mock admin tokens stay harness-only.
        """
        return self._read_env_tsv(task.get("harness_env_tsv_path"))

    @staticmethod
    def _read_env_tsv(env_tsv_path: Optional[str]) -> Dict[str, str]:
        if not env_tsv_path:
            return {}
        out: Dict[str, str] = {}
        for line in Path(env_tsv_path).read_text(encoding="utf-8").splitlines():
            key = line.strip()
            if not key or key.startswith("#"):
                continue
            value = os.environ.get(key, "")
            if value:
                out[key] = value
        return out

    def setup_workspace(self, runtime: "BaseSandboxRuntime", task: Dict[str, Any]) -> None:
        ws = self.container_workspace
        self._ensure_workspace(runtime)
        self._ensure_harness_state(runtime, task)
        self._stage_env(runtime, task)

        exec_dir = task.get("exec_dir")
        if not exec_dir:
            logger.info("uda-gym: %s has no exec/ inputs", task.get("task_name"))
            return

        exec_path = Path(exec_dir)
        for entry in sorted(exec_path.iterdir()):
            dest = f"{ws.rstrip('/')}/{entry.name}"
            ok = runtime.copy_to_runtime(str(entry), dest)
            if not ok:
                raise RuntimeError(f"uda-gym: failed to stage {entry} -> {dest}")
            logger.info("uda-gym: staged %s -> %s", entry.name, dest)

    def run_warmup(self, runtime: "BaseSandboxRuntime", task: Dict[str, Any]) -> None:
        setup_path = task.get("setup_path")
        if not setup_path:
            return
        self._ensure_harness_state(runtime, task)
        extra_cleanup: list[str] = []
        hidden_dir = task.get("hidden_dir")
        if hidden_dir:
            hidden_remote = f"{self.container_workspace.rstrip('/')}/.uda_hidden"
            runtime.exec_in_runtime(
                f"rm -rf {shlex.quote(hidden_remote)}",
                workdir=self.container_workspace,
                timeout=30,
            )
            if not runtime.copy_to_runtime(str(hidden_dir), hidden_remote):
                raise RuntimeError("uda-gym: failed to stage hidden setup assets")
            extra_cleanup.append(hidden_remote)
        self._run_hidden_script(
            runtime=runtime,
            local_path=Path(setup_path),
            remote_name=".uda_setup.sh",
            task=task,
            phase="setup",
            timeout=600,
            fail_on_error=True,
            extra_cleanup=extra_cleanup,
        )

    def inject_ground_truth(self, runtime: "BaseSandboxRuntime", task: Dict[str, Any]) -> None:
        gt_dir = task.get("gt_dir")
        if not gt_dir:
            logger.info("uda-gym: %s has no gt/ bundle", task.get("task_name"))
            return

        ws = self.container_workspace.rstrip("/")
        target = f"{ws}/gt"
        result = runtime.exec_in_runtime(
            f"mkdir -p {shlex.quote(target)}",
            workdir=ws,
            timeout=30,
        )
        if result.get("returncode", result.get("exit_code", 0)) != 0:
            raise RuntimeError(
                "uda-gym: failed to create gt dir: "
                + str(result.get("error") or result.get("output") or "<no output>")
            )

        gt_path = Path(gt_dir)
        for entry in sorted(gt_path.iterdir()):
            dest = f"{target}/{entry.name}"
            ok = runtime.copy_to_runtime(str(entry), dest)
            if not ok:
                raise RuntimeError(f"uda-gym: failed to inject {entry} -> {dest}")
        logger.info("uda-gym: injected %d gt entries into %s", sum(1 for _ in gt_path.iterdir()), target)

    def score(
        self,
        runtime: "BaseSandboxRuntime",
        task: Dict[str, Any],
        rollout_result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        check_path = task.get("check_path")
        if not check_path:
            logger.info("uda-gym: %s has no check.sh; skipping eval", task.get("task_name"))
            return None

        self._ensure_harness_state(runtime, task)
        self.inject_ground_truth(runtime, task)
        result = self._run_hidden_script(
            runtime=runtime,
            local_path=Path(check_path),
            remote_name=".uda_check.sh",
            task=task,
            phase="check",
            timeout=task.get("timeout_seconds", 600),
            fail_on_error=False,
            cleanup=False,
        )
        output = result.get("output", "")
        parsed = self._parse_json_from_output(output)
        if parsed is not None:
            if result.get("returncode", result.get("exit_code", 0)) != 0:
                parsed.setdefault("error", "check.sh exited non-zero")
            return parsed

        err = result.get("error") or output or "<no output>"
        if result.get("returncode", result.get("exit_code", 0)) != 0:
            return {"error": f"check.sh failed: {err}"}
        return {"error": f"unparseable check.sh output: {output!r}"}

    def _ensure_workspace(self, runtime: "BaseSandboxRuntime") -> None:
        ws = self.container_workspace.rstrip("/")
        result = runtime.exec_in_runtime(
            (
                f"mkdir -p {shlex.quote(ws)} {shlex.quote(f'{ws}/results')} "
                f"&& chmod u+rwX {shlex.quote(ws)} {shlex.quote(f'{ws}/results')} "
                f"&& test -w {shlex.quote(ws)} && test -w {shlex.quote(f'{ws}/results')} "
                "&& echo __UDA_WORKSPACE_OK__"
            ),
            workdir="/",
            timeout=30,
        )
        if "__UDA_WORKSPACE_OK__" not in (result.get("output") or ""):
            err = result.get("error") or result.get("output") or "<no output>"
            raise RuntimeError(f"uda-gym: workspace is not writable: {err}")

    def _stage_env(self, runtime: "BaseSandboxRuntime", task: Dict[str, Any]) -> None:
        extra_env = self.get_extra_env(task)
        if not extra_env:
            return
        exports = "\n".join(
            f"export {key}={shlex.quote(value)}" for key, value in extra_env.items()
        ) + "\n"
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh") as tmp:
            tmp.write(exports)
            tmp_path = tmp.name
        try:
            runtime.copy_to_runtime(tmp_path, "/etc/profile.d/uda_env.sh")
            runtime.copy_to_runtime(tmp_path, f"{self.container_workspace.rstrip('/')}/.env")
            logger.info("uda-gym: staged %d env var(s)", len(extra_env))
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _ensure_harness_state(
        self,
        runtime: "BaseSandboxRuntime",
        task: Dict[str, Any],
    ) -> str:
        """Create a per-rollout hidden metadata directory for setup/check only.

        The path is intentionally randomized per task execution and is only
        injected into hidden setup/check script environments. It is not written
        to /tmp_workspace, profile.d, task instructions, or the agent process
        environment.
        """
        existing = task.get("_uda_gym_harness_state_dir")
        if isinstance(existing, str) and existing:
            return existing

        task_id = str(task.get("task_id") or task.get("task_name") or "task")
        safe_task_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in task_id)
        run_id = uuid.uuid4().hex
        state_dir = f"/tmp/.uda_gym_harness/{safe_task_id}/{run_id}"
        task["_uda_gym_harness_state_dir"] = state_dir

        result = runtime.exec_in_runtime(
            f"mkdir -p {shlex.quote(state_dir)} && chmod 700 {shlex.quote(state_dir)}",
            workdir="/",
            timeout=30,
        )
        if result.get("returncode", result.get("exit_code", 0)) != 0:
            err = result.get("error") or result.get("output") or "<no output>"
            raise RuntimeError(f"uda-gym: failed to create harness state dir: {err}")
        return state_dir

    def _run_hidden_script(
        self,
        runtime: "BaseSandboxRuntime",
        local_path: Path,
        remote_name: str,
        task: Dict[str, Any],
        phase: str,
        timeout: int,
        fail_on_error: bool,
        cleanup: bool = True,
        extra_cleanup: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        ws = self.container_workspace.rstrip("/")
        remote_path = f"{ws}/{remote_name}"
        if not runtime.copy_to_runtime(str(local_path), remote_path):
            raise RuntimeError(f"uda-gym: failed to push {phase} script")

        command = f"bash {shlex.quote(remote_path)}"
        if cleanup:
            cleanup_targets = [remote_path] + list(extra_cleanup or [])
            cleanup_cmd = " ".join(shlex.quote(target) for target in cleanup_targets)
            command = f"{command}; rc=$?; rm -rf {cleanup_cmd}; exit $rc"
        result = runtime.exec_in_runtime(
            command,
            workdir=ws,
            timeout=timeout,
            env=self._hidden_script_env(task),
        )
        returncode = result.get("returncode", result.get("exit_code", 0))
        if fail_on_error and returncode != 0:
            err = result.get("error") or result.get("output") or "<no output>"
            raise RuntimeError(f"uda-gym: {phase}.sh failed: {err}")
        return result

    def _hidden_script_env(self, task: Dict[str, Any]) -> Dict[str, str]:
        env = self.get_harness_env(task)
        state_dir = task.get("_uda_gym_harness_state_dir")
        if isinstance(state_dir, str) and state_dir:
            env["UDA_GYM_HARNESS_STATE_DIR"] = state_dir
            env["UDA_GYM_TASK_ID"] = str(task.get("task_id") or task.get("task_name") or "")
        return env

    @staticmethod
    def _parse_json_from_output(output: str) -> Optional[Dict[str, Any]]:
        for line in reversed(output.splitlines()):
            text = line.strip()
            if not text:
                continue
            try:
                loaded = json.loads(text)
            except ValueError:
                continue
            if isinstance(loaded, dict):
                return loaded
        return None
