"""Codex CLI agent adapted for NanoRollout installed-agent runs."""

from __future__ import annotations

import json
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from nanorollout.envs.shell_env.base import ShellEnvironment

from .installed_agent import CliFlag, InstalledAgentBase


class CodexCli(InstalledAgentBase):
    """OpenAI Codex CLI agent."""

    CLI_FLAGS = [
        CliFlag("search", cli="--search", type="bool", default=False),
    ]

    def __init__(
        self,
        *,
        auth_json_path: str | None = None,
        codex_home: str = "/home/user/.codex",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.auth_json_path = Path(auth_json_path or "~/.codex/auth.json").expanduser()
        self.codex_home = PurePosixPath(codex_home)

    @staticmethod
    def name() -> str:
        return "codex"

    def get_version_command(self) -> str | None:
        return ". ~/.nvm/nvm.sh; codex --version"

    def install(self, environment: ShellEnvironment) -> None:
        self._stage_auth(environment)
        version_spec = f"@{self._version}" if self._version else "@latest"
        self.exec(
            environment,
            (
                "set -euo pipefail; "
                "if [ ! -s \"$HOME/.nvm/nvm.sh\" ]; then "
                "  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh -o /tmp/nvm-install.sh; "
                "  bash /tmp/nvm-install.sh; "
                "fi; "
                "export NVM_DIR=\"$HOME/.nvm\"; "
                ". \"$NVM_DIR/nvm.sh\"; "
                "nvm install 22; "
                "nvm use 22; "
                f"npm install -g @openai/codex{version_spec}; "
                "codex --version"
            ),
            timeout_sec=self.install_timeout_sec,
        )

    def _stage_auth(self, environment: ShellEnvironment) -> None:
        if not self.auth_json_path.is_file():
            raise FileNotFoundError(f"Codex auth file not found: {self.auth_json_path}")
        self.exec(
            environment,
            f"mkdir -p {shlex.quote(self.codex_home.as_posix())}",
            timeout_sec=30,
        )
        runtime = getattr(environment, "_runtime", None)
        if runtime is None or not hasattr(runtime, "copy_to_runtime"):
            raise RuntimeError("Codex auth staging requires a UDA runtime copy_to_runtime hook")
        remote_auth = self.codex_home / "auth.json"
        if not runtime.copy_to_runtime(str(self.auth_json_path), remote_auth.as_posix()):
            raise RuntimeError("Failed to stage Codex auth.json into runtime")
        self.exec(
            environment,
            f"chmod 700 {shlex.quote(self.codex_home.as_posix())} && chmod 600 {shlex.quote(remote_auth.as_posix())}",
            timeout_sec=30,
        )

    def invoke(
        self,
        instruction: str,
        environment: ShellEnvironment,
        *,
        timeout_sec: int | None = None,
    ) -> None:
        prompt_path = self.remote_logs_dir / "prompt.txt"
        output_path = self.remote_logs_dir / "codex.jsonl"
        last_message_path = self.remote_logs_dir / "last-message.txt"
        prompt_json = json.dumps(instruction)

        model_arg = ""
        if self.model_name and self.model_name not in {"default", "codex"}:
            model_arg = f"--model {shlex.quote(self.model_name)} "
        cli_flags = self.build_cli_flags()
        cli_flags_arg = f"{cli_flags} " if cli_flags else ""

        self.exec(
            environment,
            (
                f"python3 - <<'PY'\n"
                "import pathlib\n"
                f"pathlib.Path({prompt_path.as_posix()!r}).write_text({prompt_json}, encoding='utf-8')\n"
                "PY\n"
                "export NVM_DIR=\"$HOME/.nvm\"; "
                ". \"$NVM_DIR/nvm.sh\"; "
                f"export CODEX_HOME={shlex.quote(self.codex_home.as_posix())}; "
                f"codex exec --json --skip-git-repo-check "
                f"--dangerously-bypass-approvals-and-sandbox "
                f"{cli_flags_arg}{model_arg}"
                f"--cd {shlex.quote(environment.workspace_dir)} "
                f"--output-last-message {shlex.quote(last_message_path.as_posix())} "
                f"- < {shlex.quote(prompt_path.as_posix())} "
                f"2>&1 | stdbuf -oL tee {shlex.quote(output_path.as_posix())}"
            ),
            timeout_sec=timeout_sec,
        )

    def _read_jsonl_events(self) -> list[dict[str, Any]]:
        path = self.logs_dir / "codex.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                value = {"type": "stdout", "text": stripped}
            if isinstance(value, dict):
                events.append(value)
        return events

    @staticmethod
    def _event_text(event: dict[str, Any]) -> str:
        for key in ("message", "text", "delta", "output", "content"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def build_trajectory(self) -> dict[str, Any] | None:
        events = self._read_jsonl_events()
        last_message_path = self.logs_dir / "last-message.txt"
        last_message = (
            last_message_path.read_text(encoding="utf-8", errors="replace").strip()
            if last_message_path.exists()
            else ""
        )
        if not events and not last_message:
            return None

        steps: list[dict[str, Any]] = []
        step_id = 1
        prompt_path = self.logs_dir / "prompt.txt"
        if prompt_path.exists():
            steps.append({
                "step_id": step_id,
                "source": "user",
                "message": prompt_path.read_text(encoding="utf-8", errors="replace"),
            })
            step_id += 1

        for event in events:
            event_type = str(event.get("type") or "")
            text = self._event_text(event)
            if not text:
                continue
            if "user" in event_type:
                source = "user"
            elif "error" in event_type:
                source = "system"
            elif event_type == "stdout":
                source = "system"
            else:
                source = "agent"
            steps.append({
                "step_id": step_id,
                "source": source,
                "message": text,
                "event_type": event_type,
            })
            step_id += 1

        if last_message and not any(
            step.get("source") == "agent" and step.get("message") == last_message
            for step in steps
        ):
            steps.append({
                "step_id": step_id,
                "source": "agent",
                "message": last_message,
                "event_type": "last_message",
            })

        return {
            "session_id": None,
            "agent": {
                "name": self.name(),
                "version": self.version(),
                "events": len(events),
            },
            "steps": steps,
            "final_metrics": {
                "total_cost_usd": 0.0,
            },
        }
