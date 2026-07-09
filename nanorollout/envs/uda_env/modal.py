"""Modal-backed sandbox runtime for UDA tasks.

Constructs a ``modal.Image.from_dockerfile`` against the per-task
``Dockerfile`` (already FROM uda-desktop after migration), spawns a
``modal.Sandbox`` with ``encrypted_ports=[8080]``, and exposes the
sandbox's tunnel URL for the agent loop. Identical to
cocoa_env.modal at the runtime layer.
"""

from __future__ import annotations

import hashlib
import os
import time
import modal

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseSandboxRuntime, runtime_logger


def _prepare_rest_lines(rest_lines: List[str]) -> List[str]:
    """Patch a per-task Dockerfile's commands for Modal compatibility.

    Three ports of the uda-desktop image expectations:

    - Some per-task Dockerfiles run bare ``pip install`` but the base
      image only ships ``/usr/bin/python3`` — no ``pip`` on ``PATH``.
      Inject ``apt-get install -y python3-pip`` before the first such
      line. Skip if the user already installed python3-pip themselves.
    - Replace bare ``pip`` invocations with ``python3 -m pip`` so they
      run via the system Python after we install pip via apt.
    - Modal's ``dockerfile_commands`` parser rejects ``COPY --chown=...``
      flags. Rewrite to plain ``COPY`` + a follow-up ``RUN chown -R``.
    """
    import re as _re

    rewritten: List[str] = []
    for line in rest_lines:
        m = _re.match(
            r"^(\s*)COPY\s+--chown=([^\s]+)\s+(.+)$", line
        )
        if m:
            indent, ownership, args = m.groups()
            rewritten.append(f"{indent}COPY {args}")
            target = args.split()[-1]
            rewritten.append(f"{indent}RUN chown -R {ownership} {target}")
        else:
            rewritten.append(line)
    rest_lines = rewritten
    needs_pip = any(
        line.lstrip().startswith(("RUN pip ", "RUN pip3 "))
        for line in rest_lines
    )
    if not needs_pip:
        return list(rest_lines)
    already_installs_pip = any(
        "python3-pip" in line and "apt" in line for line in rest_lines
    )
    out: List[str] = []
    if not already_installs_pip:
        out.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "python3-pip && rm -rf /var/lib/apt/lists/*"
        )
    for line in rest_lines:
        s = line.lstrip()
        if s.startswith("RUN pip "):
            line = line.replace("RUN pip ", "RUN python3 -m pip ", 1)
        elif s.startswith("RUN pip3 "):
            line = line.replace("RUN pip3 ", "RUN python3 -m pip ", 1)
        out.append(line)
    return out


def _parse_dockerfile(path: Path) -> Tuple[str, List[str]]:
    """Split a per-task Dockerfile into ``(FROM image, remaining lines)``.

    Used by the registry-secret code path: Modal's ``Image.from_registry``
    handles the pull (with auth) and ``Image.dockerfile_commands`` applies
    everything else. The per-task Dockerfiles in ``adapter/<bench>/`` are
    deliberately tiny (5–10 lines) and always start with ``FROM``, so a
    line-based parser is enough — no need for a real Dockerfile grammar.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    base_image: Optional[str] = None
    rest: List[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if base_image is None and stripped.upper().startswith("FROM "):
            tokens = stripped.split()
            # Skip ``--platform=...`` flag if present.
            image_tok = next(
                (tok for tok in tokens[1:] if not tok.startswith("--")), None
            )
            if image_tok is None:
                raise ValueError(f"Could not parse FROM line in {path}")
            base_image = image_tok
            continue
        rest.append(raw)
    if base_image is None:
        raise ValueError(f"No FROM line in Dockerfile {path}")
    return base_image, rest


class ModalSandboxRuntime(BaseSandboxRuntime):
    """Lifecycle manager backed by Modal sandboxes."""

    runtime_type = "modal"

    def __init__(self, client):
        super().__init__(client)
        self.sandbox: Optional[Any] = None
        self.app: Optional[Any] = None
        self.service_port = 8080

    def start(self, task: Dict[str, Any], wait_time: int = 60) -> bool:
        try:
            task_dir = task.get("task_dir")
            task_name = task.get("task_name", "task")
            if not task_dir:
                runtime_logger.error("Task object must contain 'task_dir' key")
                return False

            task_path = Path(task_dir)
            dockerfile_path = task_path / "Dockerfile"
            if not dockerfile_path.exists():
                runtime_logger.error("Task '%s' is missing Dockerfile at %s", task_name, dockerfile_path)
                return False

            self.client.task_name = task_name
            self.client.task_dir = task_dir
            self.service_port = int(self.client.sandbox_config.get("modal_container_port", 8080))

            app_name = self.client.sandbox_config.get("modal_app_name", "__nanorollout_uda__")
            bench = (self.client.sandbox_config.get("bench") or "uda").strip() or "uda"
            # Modal sandbox names are capped at 64 chars. Bench + raw
            # task_name + epoch can overflow (e.g. wildclaw's long
            # ``06_Safety_Alignment_task_*`` ids), so fall back to a
            # task-name hash when the natural name is too long.
            default_name = f"uda-{bench}-{task_name}-{int(time.time())}"
            if len(default_name) >= 64:
                digest = hashlib.sha256(task_name.encode("utf-8")).hexdigest()[:10]
                default_name = f"uda-{bench}-{digest}-{int(time.time())}"
            sandbox_name = (
                self.client.sandbox_config.get("modal_sandbox_name")
                or default_name
            )
            startup_timeout = int(self.client.sandbox_config.get("modal_startup_timeout", 300))
            sandbox_timeout = int(self.client.sandbox_config.get("modal_timeout", 3600))
            idle_timeout = self.client.sandbox_config.get("modal_idle_timeout", 600)

            self.app = modal.App.lookup(app_name, create_if_missing=True)

            # If a registry secret is configured (e.g. private ghcr image),
            # Modal's ``Image.from_dockerfile`` can't authenticate the FROM
            # line — it has no ``secret=`` knob. Reconstruct the image as
            # ``from_registry(base, secret).dockerfile_commands(rest)``
            # so the base image pull carries credentials.
            registry_secret_name = (
                self.client.sandbox_config.get("modal_registry_secret")
                or os.environ.get("UDA_MODAL_REGISTRY_SECRET")
            )
            if registry_secret_name:
                base_image, rest_lines = _parse_dockerfile(dockerfile_path)
                secret = modal.Secret.from_name(registry_secret_name)
                image = modal.Image.from_registry(base_image, secret=secret)
                if rest_lines:
                    rest_lines = _prepare_rest_lines(rest_lines)
                    image = image.dockerfile_commands(
                        rest_lines,
                        context_dir=str(task_path.resolve()),
                    )
            else:
                image = modal.Image.from_dockerfile(
                    str(dockerfile_path.resolve()),
                    context_dir=str(task_path.resolve()),
                )

            create_kwargs: Dict[str, Any] = {
                "app": self.app,
                "image": image,
                "encrypted_ports": [self.service_port],
                "timeout": sandbox_timeout,
                "name": sandbox_name,
            }
            if idle_timeout is not None:
                create_kwargs["idle_timeout"] = int(idle_timeout)

            region = self.client.sandbox_config.get("modal_region")
            if region:
                create_kwargs["region"] = region

            cpu = self.client.sandbox_config.get("modal_cpu", 1)
            if cpu is not None:
                create_kwargs["cpu"] = cpu

            memory = self.client.sandbox_config.get("modal_memory", 2048)
            if memory is not None:
                create_kwargs["memory"] = memory

            runtime_logger.info(
                "Starting Modal sandbox for task '%s' (app=%s, dockerfile=%s)",
                task_name,
                app_name,
                dockerfile_path,
            )
            self.sandbox = modal.Sandbox.create(**create_kwargs)

            tunnel = self.sandbox.tunnels()[self.service_port]
            self.client.set_base_url(tunnel.url)
            self.client.runtime_id = getattr(self.sandbox, "object_id", None)
            self.client.container_id = self.client.runtime_id
            self.client._update_runtime_metadata(
                sandbox_id=self.client.runtime_id,
                app_name=app_name,
                sandbox_name=sandbox_name,
                task_name=task_name,
                task_dir=task_dir,
                container_port=self.service_port,
                bench=bench,
                uda_image=self.client.sandbox_config.get("uda_image"),
                corpus_revision=self.client.sandbox_config.get("corpus_revision"),
            )

            health_timeout = max(wait_time, startup_timeout)
            if self._wait_for_health(health_timeout):
                runtime_logger.info("Modal sandbox environment ready")
                return True

            runtime_logger.error(
                "Modal sandbox environment failed to become ready within timeout of %s seconds",
                health_timeout,
            )
            self.cleanup()
            return False
        except Exception as e:
            runtime_logger.error("Error creating Modal sandbox: %s", e)
            self.cleanup()
            return False

    def cleanup(self) -> bool:
        if self.sandbox is None:
            runtime_logger.info("No Modal sandbox to clean up")
            return True

        try:
            sandbox_id = getattr(self.sandbox, "object_id", None)
            runtime_logger.info("Terminating Modal sandbox %s", sandbox_id or "<unknown>")
            self.sandbox.terminate()
            self.client.container_id = None
            self.client.runtime_id = None
            return True
        except Exception as e:
            runtime_logger.error("Error terminating Modal sandbox: %s", e)
            return False
        finally:
            self.sandbox = None

    # copy_to_runtime + exec_in_runtime inherit the SDK-mediated default
    # impl from BaseSandboxRuntime, which talks HTTP to the sandbox
    # server through ``client.sdk_client``. The Modal tunnel URL is
    # exposed at ``client.set_base_url(tunnel.url)`` so the SDK calls
    # land on the right endpoint inside the modal container.
