"""Shim presenting a started uda-env runtime as a ``ShellEnvironment``.

Lets ``InstalledAgentBase``-style CLI agents (Claude Code, Qwen Code,
OpenCode) run on top of a uda-desktop sandbox without reinventing the
lifecycle: ``TaskExecutor.setup_environment`` still owns container
creation, driver workspace staging, and teardown; this adapter just
exposes the narrow ``execute / start / stop / is_running / get_git_diff``
surface that ``InstalledAgentBase.run`` calls.

The CLI-agent path is intentionally CLI-only — Claude Code in
``/home/kasm-user`` operates on the filesystem via its native
Bash/Edit/Read tools. GUI primitives (screenshot / click / type) are
not exposed here; bridging them through MCP is a follow-up.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Optional

from nanorollout.envs.shell_env.base import ExecutionResult, ShellEnvironment

from .base import UDA_WORKSPACE, BaseSandboxRuntime


_RC_SENTINEL = "__NRO_RC_SENTINEL__"
_HEARTBEAT_MARKER = "__NRO_HEARTBEAT__"
_HEARTBEAT_INTERVAL_SEC = 10


def _strip_heartbeats(output: str) -> str:
    """Drop the heartbeat marker lines our execute() wrapper injects.

    The marker contains ``_`` (not in the base64 alphabet) so leaving it
    in would corrupt the tar+base64 fallback in
    ``InstalledAgentBase._sync_remote_logs``. We filter here so every
    caller sees clean stdout, not just the SDK-pull path.
    """
    if _HEARTBEAT_MARKER not in output:
        return output
    return "\n".join(
        line for line in output.splitlines()
        if _HEARTBEAT_MARKER not in line
    )


class UdaShellAdapter(ShellEnvironment):
    """Wrap an already-started :class:`BaseSandboxRuntime`.

    Lifecycle methods are no-ops — the uda-env ``TaskExecutor`` is
    responsible for ``create_environment`` / ``cleanup_environment``.
    ``execute`` round-trips through the runtime's SDK-mediated
    ``exec_in_runtime`` and recovers the real exit code by tail-printing
    a sentinel-delimited ``$?`` (the SDK only surfaces a coarse
    success/failure code natively).
    """

    def __init__(
        self,
        runtime: BaseSandboxRuntime,
        *,
        workspace_dir: str = UDA_WORKSPACE,
        timeout: int = 600,
    ) -> None:
        self._runtime = runtime
        self.workspace_dir = workspace_dir
        self.timeout = int(timeout)
        self._running = True

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def execute(self, command: str, timeout: Optional[int] = None) -> ExecutionResult:
        effective_timeout = int(timeout) if timeout is not None else self.timeout
        # Run the user command in a subshell so a bare ``exit`` inside it
        # only terminates the subshell, not the long-running SDK session.
        # Brace groups (``{ ...; }``) share the parent shell and would tear
        # the session down on ``exit``, taking the sentinel echo with them.
        #
        # Heartbeat: the uda-env SDK server enforces a ~120s "no-output"
        # idle cap on shell sessions and ignores our explicit
        # ``no_change_timeout``. Long LLM calls inside ``claude --print``
        # routinely go quiet for that long, getting killed mid-rollout. We
        # spawn a background heartbeat that prints a marker line while the
        # user command is alive; the host-side
        # ``InstalledAgentBase`` only consumes the file Claude itself tee'd,
        # so the heartbeat lines are harmless noise in the SDK output.
        # Redirect the actual command output to a regular file while it
        # runs. Some CLIs spawn helper processes that inherit stdout/stderr;
        # if those FDs point at the HTTP server pipe, subprocess.run() in the
        # compat server can wait forever for EOF even after the main command
        # exits. A temp file decouples those descendants from the response
        # pipe; heartbeat + rc sentinel still stream through stdout.
        wrapped = (
            "__nro_out=$(mktemp /tmp/nanorollout-shell.XXXXXX) || exit 1; "
            "{ "
            f"( {command}\n) >\"$__nro_out\" 2>&1 &\n"
            "__nro_main_pid=$!; "
            "( while kill -0 $__nro_main_pid 2>/dev/null; do "
            f"printf '{_HEARTBEAT_MARKER}\\n'; sleep {_HEARTBEAT_INTERVAL_SEC}; done ) &\n"
            "__nro_hb_pid=$!; "
            "wait $__nro_main_pid; "
            "__nro_rc=$?; "
            "kill $__nro_hb_pid 2>/dev/null; "
            "wait $__nro_hb_pid 2>/dev/null; "
            "}; "
            f"printf '\\n%s%d\\n' '{_RC_SENTINEL}' \"$__nro_rc\"; "
            "cat \"$__nro_out\"; "
            "rm -f \"$__nro_out\""
        )
        result = self._runtime.exec_in_runtime(
            wrapped,
            workdir=self.workspace_dir,
            timeout=effective_timeout,
        )
        output = result.get("output", "") or ""
        sdk_rc = result.get("returncode", -1)

        if _RC_SENTINEL in output:
            head, _, tail = output.partition(_RC_SENTINEL)
            tail_lines = tail.splitlines(True)
            try:
                exit_code = int(tail_lines[0].strip())
            except (ValueError, IndexError):
                exit_code = 0
            command_tail = "".join(tail_lines[1:])
            visible_output = command_tail if command_tail else head
            return ExecutionResult(
                output=_strip_heartbeats(visible_output).rstrip("\n"),
                exit_code=exit_code,
            )

        if sdk_rc != 0 and result.get("error"):
            return ExecutionResult(output=result.get("error", ""), exit_code=-1)
        return ExecutionResult(output=_strip_heartbeats(output), exit_code=sdk_rc)

    def pull_dir_via_sdk(self, remote_dir: str, local_dir: Path) -> bool:
        """Fetch a remote directory via the file SDK, bypassing shell-exec
        output caps and shell-level FS oddities.

        ``InstalledAgentBase._sync_remote_logs`` defaults to
        ``tar | base64 | tr -d '\\n'`` over a shell session, which the
        uda-env SDK truncates at ~30 KB — Claude Code session JSONLs are
        bigger than that. ``find`` over the same shell also returns weird
        recursive paths inside Claude Code's cache. We instead drive the
        SDK's structured ``file.list_path`` + ``file.download_file`` API,
        which sees the same files cleanly.

        Skips: ``tool-results/`` (WebFetch binary artifacts that bloat
        sync) and ``backups/`` (rotated config snapshots). Also defence-
        skips any path that re-includes the remote_dir base name, which
        is the fingerprint of a pathological self-loop.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)
        client = self._runtime.client
        sdk = getattr(client, "sdk_client", None)
        if sdk is None:
            init = getattr(client, "_initialize_sdk_client", None)
            if init is not None:
                init()
                sdk = getattr(client, "sdk_client", None)
        if sdk is None:
            return False

        try:
            listing = sdk.file.list_path(
                path=remote_dir, recursive=True, max_depth=12
            )
        except Exception as exc:
            _log.warning("pull_dir_via_sdk: list_path(%s) failed: %s", remote_dir, exc)
            return False

        entries = getattr(listing.data, "files", None) or []
        # Files only — skip directory entries.
        file_paths = [
            e.path for e in entries
            if not getattr(e, "is_directory", False) and getattr(e, "path", None)
        ]
        _log.info(
            "pull_dir_via_sdk: %s -> %d files (total entries=%d)",
            remote_dir,
            len(file_paths),
            len(entries),
        )

        local_dir.mkdir(parents=True, exist_ok=True)
        prefix = remote_dir.rstrip("/") + "/"
        base_name = Path(remote_dir).name
        downloaded = 0
        skipped = 0
        for remote_path in file_paths:
            rel = (
                remote_path[len(prefix):]
                if remote_path.startswith(prefix)
                else Path(remote_path).name
            )
            # Skip large/uninteresting subtrees + self-loop fingerprints.
            if "/tool-results/" in rel or "/backups/" in rel:
                skipped += 1
                continue
            if rel.count(base_name) >= 1:
                skipped += 1
                continue
            if len(rel) > 500 or rel.count("/") > 12:
                skipped += 1
                continue
            dest = local_dir / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                skipped += 1
                continue
            try:
                with open(dest, "wb") as fh:
                    for chunk in sdk.file.download_file(path=remote_path):
                        fh.write(chunk)
                downloaded += 1
            except Exception as exc:
                _log.warning("pull_dir_via_sdk: download %s failed: %s", remote_path, exc)
                skipped += 1
                continue
        _log.info("pull_dir_via_sdk: downloaded=%d skipped=%d", downloaded, skipped)
        # Once list_path returned entries, prefer this path's outcome over
        # the tar+base64 fallback — that fallback would re-traverse the same
        # weird remote fs and likely hit the SDK's ~30 KB output cap.
        return True
