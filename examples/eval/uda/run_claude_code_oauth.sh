#!/usr/bin/env bash
# Run a single UDA-bench task through Claude Code (CLI-only baseline path).
#
# Spins up a uda-env sandbox (docker / modal / ec2), hands it off to a
# self-driving `claude --print` invocation, then scores via the per-bench
# driver (uda-gym's check.sh, wildclaw-v1's grade.py, or cocoa-v1's test.py).
# Claude Code uses
# its native Bash / Edit / Read tools through the sandbox shell/file API.
#
# Auth: CLAUDE_CODE_OAUTH_TOKEN (preferred), ANTHROPIC_API_KEY, or
# AWS_BEARER_TOKEN_BEDROCK for Claude Code Bedrock mode.
set -euo pipefail

if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -z "${ANTHROPIC_API_KEY:-}" && -z "${AWS_BEARER_TOKEN_BEDROCK:-}" ]]; then
  cat >&2 <<'EOF'
CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, or AWS_BEARER_TOKEN_BEDROCK is required.

Example:
  CLAUDE_CODE_OAUTH_TOKEN="<oauth-token>" \
  BENCH="uda-gym" \
  INSTANCE_ID="codex100_002" \
  bash examples/eval/uda/run_claude_code_oauth.sh
EOF
  exit 1
fi

BENCH="${BENCH:-uda-gym}"
INSTANCE_ID="${INSTANCE_ID:-codex100_002}"
REQUEST_FILE="${REQUEST_FILE:-}"

MODEL_NAME="${MODEL_NAME:-claude-sonnet-4-6}"
BASE_URL="${ANTHROPIC_BASE_URL:-${BASE_URL:-}}"
AGENT="${AGENT:-claude-code}"

OUTPUT_DIR="${OUTPUT_DIR:-./results/uda-${BENCH}-${AGENT}}"
CONCURRENCY="${CONCURRENCY:-1}"
ENV_TYPE="${ENV_TYPE:-modal}"
EC2_REGION="${EC2_REGION:-ap-southeast-1}"
EC2_LAUNCH_TEMPLATE_ID="${EC2_LAUNCH_TEMPLATE_ID:-}"
EC2_LAUNCH_TEMPLATE_NAME="${EC2_LAUNCH_TEMPLATE_NAME:-}"
EC2_LAUNCH_TEMPLATE_VERSION="${EC2_LAUNCH_TEMPLATE_VERSION:-}"
EC2_AMI_ID="${EC2_AMI_ID:-}"
EC2_INSTANCE_TYPE="${EC2_INSTANCE_TYPE:-}"
EC2_SUBNET_ID="${EC2_SUBNET_ID:-}"
EC2_SECURITY_GROUP_IDS="${EC2_SECURITY_GROUP_IDS:-}"
EC2_IAM_INSTANCE_PROFILE="${EC2_IAM_INSTANCE_PROFILE:-}"
EC2_WORKSPACE_DIR="${EC2_WORKSPACE_DIR:-/home/user}"
EC2_TERMINATE_ON_CLEANUP="${EC2_TERMINATE_ON_CLEANUP:-true}"
EC2_ENV_PROFILE="${EC2_ENV_PROFILE:-general-root}"

UDA_TASKS_DIR="${UDA_TASKS_DIR:-}"
# Native uda-gym and wildclaw-v1 tasks are unencrypted; cocoa-v1 ships .enc.
if [[ "${BENCH}" == "cocoa-v1" ]]; then
  USE_ENCRYPTED_TASKS="${USE_ENCRYPTED_TASKS:-true}"
else
  USE_ENCRYPTED_TASKS="${USE_ENCRYPTED_TASKS:-false}"
fi

# Installed-agent path doesn't drive the per-step controller loop, so the
# usual --client-type / --max-iterations knobs are inert; we still pass
# --client-type unified to keep sandbox_client construction identical to
# the run_uda_bench.sh control path.
CLIENT_TYPE="${CLIENT_TYPE:-unified}"

STEP_TIMEOUT="${STEP_TIMEOUT:-1800}"     # per-shell-command ceiling
AGENT_TIMEOUT="${AGENT_TIMEOUT:-1800}"   # full `claude --print` wall budget
EVAL_TIMEOUT="${EVAL_TIMEOUT:-1800}"
ENV_TIMEOUT="${ENV_TIMEOUT:-180}"
CREATE_TIMEOUT="${CREATE_TIMEOUT:-600}"

cmd=(
  nro run
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
)

if [[ -n "${BASE_URL}" ]]; then
  cmd+=(--base-url "${BASE_URL}")
fi

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  cmd+=(--api-key "${ANTHROPIC_API_KEY}")
fi

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

# CLAUDE_CODE_OAUTH_TOKEN is read by the ClaudeCode adapter directly
# from the host environment and forwarded into the container.
"${cmd[@]}"
