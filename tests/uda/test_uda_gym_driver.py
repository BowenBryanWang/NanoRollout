from __future__ import annotations

import json
from pathlib import Path

from nanorollout.envs.uda_env.driver.uda_gym import UdaGymDriver


class FakeRuntime:
    def __init__(self, *, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.commands: list[dict] = []

    def exec_in_runtime(self, command, *, workdir, timeout, env=None):
        self.commands.append(
            {
                "command": command,
                "workdir": workdir,
                "timeout": timeout,
                "env": dict(env or {}),
            }
        )
        return {"exit_code": self.exit_code, "output": ""}


def test_instruction_md_replaces_placeholder_task_yaml(tmp_path: Path) -> None:
    (tmp_path / "task.yaml").write_text(
        "instruction: See instruction.md for the agent-visible task\n",
        encoding="utf-8",
    )
    (tmp_path / "instruction.md").write_text(
        "Open the seeded dashboard and reconcile the export.",
        encoding="utf-8",
    )
    (tmp_path / "meta.json").write_text(
        json.dumps({"id": "uda-placeholder-test"}),
        encoding="utf-8",
    )

    task = UdaGymDriver().load_task(tmp_path)

    assert task["instruction"] == "Open the seeded dashboard and reconcile the export."
    assert task["task_id"] == "uda-placeholder-test"


def test_harness_state_is_randomized_per_rollout_and_reused_within_task() -> None:
    driver = UdaGymDriver()
    first_runtime = FakeRuntime()
    second_runtime = FakeRuntime()
    first_task = {"task_id": "task/one"}
    second_task = {"task_id": "task/one"}

    first_path = driver._ensure_harness_state(first_runtime, first_task)
    repeated_path = driver._ensure_harness_state(first_runtime, first_task)
    second_path = driver._ensure_harness_state(second_runtime, second_task)

    assert first_path == repeated_path
    assert first_path != second_path
    assert first_path.startswith("/tmp/.uda_gym_harness/task_one/")
    assert len(first_runtime.commands) == 1
    assert driver._hidden_script_env(first_task) == {
        "UDA_GYM_HARNESS_STATE_DIR": first_path,
        "UDA_GYM_TASK_ID": "task/one",
    }


def test_harness_state_creation_honors_exit_code_shape() -> None:
    driver = UdaGymDriver()
    runtime = FakeRuntime(exit_code=9)

    try:
        driver._ensure_harness_state(runtime, {"task_id": "broken"})
    except RuntimeError as exc:
        assert "failed to create harness state dir" in str(exc)
    else:
        raise AssertionError("non-zero exit_code must fail harness state creation")
