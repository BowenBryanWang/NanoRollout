#!/usr/bin/env bash
# Run a single UDA-bench task through Codex CLI on uda-env EC2.
set -euo pipefail

NRO_BIN="${NRO_BIN:-nro}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "${NRO_BIN}" >/dev/null 2>&1; then
  cat >&2 <<EOF
NanoRollout CLI is required but not on PATH: ${NRO_BIN}

Run from the NanoRollout repo with:
  PATH=/path/to/NanoRollout/.venv/bin:\$PATH bash examples/eval/uda/run_codex_oauth.sh
EOF
  exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python is required but not on PATH: ${PYTHON_BIN}" >&2
  exit 1
fi

CODEX_AUTH_JSON="${CODEX_AUTH_JSON:-${HOME}/.codex/auth.json}"
if [[ ! -f "${CODEX_AUTH_JSON}" ]]; then
  cat >&2 <<EOF
Codex auth file is required: ${CODEX_AUTH_JSON}

Run Codex login locally first, or set CODEX_AUTH_JSON=/path/to/auth.json.
EOF
  exit 1
fi

BENCH="${BENCH:-uda-gym}"
INSTANCE_ID="${INSTANCE_ID:-codex100_002}"
REQUEST_FILE="${REQUEST_FILE:-}"

MODEL_NAME="${MODEL_NAME:-default}"
AGENT="${AGENT:-codex}"
OUTPUT_DIR="${OUTPUT_DIR:-./results/uda-${BENCH}-${AGENT}}"
CONCURRENCY="${CONCURRENCY:-1}"

ENV_TYPE="${ENV_TYPE:-ec2}"
EC2_REGION="${EC2_REGION:-ap-southeast-1}"
EC2_LAUNCH_TEMPLATE_ID="${EC2_LAUNCH_TEMPLATE_ID:-lt-03862713037af59fe}"
EC2_LAUNCH_TEMPLATE_NAME="${EC2_LAUNCH_TEMPLATE_NAME:-}"
EC2_LAUNCH_TEMPLATE_VERSION="${EC2_LAUNCH_TEMPLATE_VERSION:-}"
EC2_AMI_ID="${EC2_AMI_ID:-}"
EC2_INSTANCE_TYPE="${EC2_INSTANCE_TYPE:-t3.xlarge}"
EC2_SUBNET_ID="${EC2_SUBNET_ID:-subnet-0c85c17f888605401}"
EC2_SECURITY_GROUP_IDS="${EC2_SECURITY_GROUP_IDS:-sg-029a65325aae8f739}"
EC2_IAM_INSTANCE_PROFILE="${EC2_IAM_INSTANCE_PROFILE:-uda-gym-ec2-instance-profile}"
EC2_WORKSPACE_DIR="${EC2_WORKSPACE_DIR:-/home/user}"
EC2_TERMINATE_ON_CLEANUP="${EC2_TERMINATE_ON_CLEANUP:-true}"
EC2_ENV_PROFILE="${EC2_ENV_PROFILE:-general-root}"

UDA_TASKS_DIR="${UDA_TASKS_DIR:-}"
if [[ "${BENCH}" == "cocoa-v1" ]]; then
  USE_ENCRYPTED_TASKS="${USE_ENCRYPTED_TASKS:-true}"
else
  USE_ENCRYPTED_TASKS="${USE_ENCRYPTED_TASKS:-false}"
fi

CLIENT_TYPE="${CLIENT_TYPE:-unified}"
STEP_TIMEOUT="${STEP_TIMEOUT:-1800}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-1800}"
EVAL_TIMEOUT="${EVAL_TIMEOUT:-1800}"
ENV_TIMEOUT="${ENV_TIMEOUT:-900}"
CREATE_TIMEOUT="${CREATE_TIMEOUT:-900}"

EXTRA_ARGS_JSON="${EXTRA_ARGS_JSON:-}"
if [[ -z "${EXTRA_ARGS_JSON}" ]]; then
  EXTRA_ARGS_JSON="$("${PYTHON_BIN}" - <<PY
import json
print(json.dumps({
    "agent_kwargs": {
        "auth_json_path": "${CODEX_AUTH_JSON}",
        "install_timeout_sec": int("${CODEX_INSTALL_TIMEOUT:-1800}"),
    },
    "sandbox_config": {
        "sdk_timeout": int("${SDK_TIMEOUT:-1800}"),
    },
}))
PY
)"
fi

if [[ -z "${REQUEST_FILE}" ]]; then
  "${PYTHON_BIN}" - "${BENCH}" "${INSTANCE_ID}" "${UDA_TASKS_DIR}" <<'PY'
import sys

from nanorollout.harness.runner.uda.uda_agent import _load_task, _resolve_task_root

bench, instance_id, tasks_dir = sys.argv[1:4]
extra_args = {"uda_tasks_dir": tasks_dir} if tasks_dir else {}
task_root, task_dir = _resolve_task_root(instance_id, extra_args, bench=bench)
task = _load_task(task_dir, False)
if task.get("driver") != "uda-gym" and bench == "uda-gym":
    raise SystemExit(f"Expected uda-gym driver for {task_dir}, got {task.get('driver')!r}")
if not task.get("instruction"):
    raise SystemExit(f"Task {task_dir} did not load an instruction")
print(
    f"Validated UDA task: bench={bench} instance_id={instance_id} "
    f"root={task_root} driver={task.get('driver')}"
)
PY
fi

cmd=(
  "${NRO_BIN}" run
  --task uda
  --agent "${AGENT}"
  --bench "${BENCH}"
  --model-name "${MODEL_NAME}"
  --env-type "${ENV_TYPE}"
  --output-dir "${OUTPUT_DIR}"
  --concurrency "${CONCURRENCY}"
  --client-type "${CLIENT_TYPE}"
  --step-timeout "${STEP_TIMEOUT}"
  --agent-timeout "${AGENT_TIMEOUT}"
  --eval-timeout "${EVAL_TIMEOUT}"
  --env-timeout "${ENV_TIMEOUT}"
  --create-timeout "${CREATE_TIMEOUT}"
  --use-encrypted-tasks "${USE_ENCRYPTED_TASKS}"
  --extra-args "${EXTRA_ARGS_JSON}"
)

if [[ -n "${REQUEST_FILE}" ]]; then
  cmd+=(--request-file "${REQUEST_FILE}")
else
  cmd+=(--instance-id "${INSTANCE_ID}")
fi

if [[ -n "${UDA_TASKS_DIR}" ]]; then
  cmd+=(--uda-tasks-dir "${UDA_TASKS_DIR}")
fi

if [[ "${ENV_TYPE}" == "ec2" ]]; then
  cmd+=(--ec2-region "${EC2_REGION}")
  cmd+=(--ec2-terminate-on-cleanup "${EC2_TERMINATE_ON_CLEANUP}")
  cmd+=(--ec2-env-profile "${EC2_ENV_PROFILE}")
  cmd+=(--ec2-workspace-dir "${EC2_WORKSPACE_DIR}")
  if [[ -n "${EC2_LAUNCH_TEMPLATE_ID}" ]]; then
    cmd+=(--ec2-launch-template-id "${EC2_LAUNCH_TEMPLATE_ID}")
  fi
  if [[ -n "${EC2_LAUNCH_TEMPLATE_NAME}" ]]; then
    cmd+=(--ec2-launch-template-name "${EC2_LAUNCH_TEMPLATE_NAME}")
  fi
  if [[ -n "${EC2_LAUNCH_TEMPLATE_VERSION}" ]]; then
    cmd+=(--ec2-launch-template-version "${EC2_LAUNCH_TEMPLATE_VERSION}")
  fi
  if [[ -n "${EC2_AMI_ID}" ]]; then
    cmd+=(--ec2-ami-id "${EC2_AMI_ID}")
  fi
  if [[ -n "${EC2_INSTANCE_TYPE}" ]]; then
    cmd+=(--ec2-instance-type "${EC2_INSTANCE_TYPE}")
  fi
  if [[ -n "${EC2_SUBNET_ID}" ]]; then
    cmd+=(--ec2-subnet-id "${EC2_SUBNET_ID}")
  fi
  if [[ -n "${EC2_SECURITY_GROUP_IDS}" ]]; then
    cmd+=(--ec2-security-group-ids "${EC2_SECURITY_GROUP_IDS}")
  fi
  if [[ -n "${EC2_IAM_INSTANCE_PROFILE}" ]]; then
    cmd+=(--ec2-iam-instance-profile "${EC2_IAM_INSTANCE_PROFILE}")
  fi
fi

if [[ "${PRINT_CMD_ONLY:-}" =~ ^(1|true|yes|on)$ ]]; then
  printf '%q ' "${cmd[@]}"
  printf '\n'
  exit 0
fi

"${cmd[@]}"
