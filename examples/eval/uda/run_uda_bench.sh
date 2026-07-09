#!/usr/bin/env bash
# Run a single migrated benchmark task on the unified uda-desktop image.
#
# The task corpus ships inside the NanoRollout package at
#   nanorollout/envs/uda_env/adapter/<bench>/<instance_id>/
# No external benchmark repo checkout is required at run time.
#
# Switch between benchmarks with BENCH (e.g. BENCH=cocoa-v1,
# BENCH=wildclaw-v1, BENCH=uda-gym).
set -euo pipefail

BENCH="${BENCH:-uda-gym}"
INSTANCE_ID="${INSTANCE_ID:-codex100_002}"
REQUEST_FILE="${REQUEST_FILE:-}"

MODEL_NAME="${MODEL_NAME:-claude-sonnet-4-6}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-${BASE_URL:-}}"
OPENAI_API_KEY="${OPENAI_API_KEY:-${API_KEY:-${ANTHROPIC_API_KEY:-}}}"

OUTPUT_DIR="${OUTPUT_DIR:-./results/uda-${BENCH}}"
CONCURRENCY="${CONCURRENCY:-1}"
ENV_TYPE="${ENV_TYPE:-modal}"
EC2_REGION="${EC2_REGION:-}"
EC2_AMI_ID="${EC2_AMI_ID:-}"
EC2_LAUNCH_TEMPLATE_ID="${EC2_LAUNCH_TEMPLATE_ID:-}"
EC2_LAUNCH_TEMPLATE_NAME="${EC2_LAUNCH_TEMPLATE_NAME:-}"
EC2_LAUNCH_TEMPLATE_VERSION="${EC2_LAUNCH_TEMPLATE_VERSION:-}"
EC2_INSTANCE_TYPE="${EC2_INSTANCE_TYPE:-}"
EC2_SUBNET_ID="${EC2_SUBNET_ID:-}"
EC2_SECURITY_GROUP_IDS="${EC2_SECURITY_GROUP_IDS:-}"
EC2_IAM_INSTANCE_PROFILE="${EC2_IAM_INSTANCE_PROFILE:-}"
EC2_USE_PRIVATE_IP="${EC2_USE_PRIVATE_IP:-}"
EC2_TERMINATE_ON_CLEANUP="${EC2_TERMINATE_ON_CLEANUP:-}"
EC2_ENV_PROFILE="${EC2_ENV_PROFILE:-}"

UDA_TASKS_DIR="${UDA_TASKS_DIR:-}"
if [[ "${BENCH}" == "cocoa-v1" ]]; then
  USE_ENCRYPTED_TASKS="${USE_ENCRYPTED_TASKS:-true}"
else
  USE_ENCRYPTED_TASKS="${USE_ENCRYPTED_TASKS:-false}"
fi
CLIENT_TYPE="${CLIENT_TYPE:-unified}"

STEP_TIMEOUT="${STEP_TIMEOUT:-600}"
EVAL_TIMEOUT="${EVAL_TIMEOUT:-1800}"
ENV_TIMEOUT="${ENV_TIMEOUT:-180}"
CREATE_TIMEOUT="${CREATE_TIMEOUT:-600}"
MAX_ITERATIONS="${MAX_ITERATIONS:-100}"

cmd=(
  nro run
  --task uda
  --agent uda-agent
  --bench "${BENCH}"
  --model-name "${MODEL_NAME}"
  --env-type "${ENV_TYPE}"
  --output-dir "${OUTPUT_DIR}"
  --concurrency "${CONCURRENCY}"
  --base-url "${OPENAI_BASE_URL}"
  --api-key "${OPENAI_API_KEY}"
  --client-type "${CLIENT_TYPE}"
  --step-timeout "${STEP_TIMEOUT}"
  --eval-timeout "${EVAL_TIMEOUT}"
  --env-timeout "${ENV_TIMEOUT}"
  --create-timeout "${CREATE_TIMEOUT}"
  --max-iterations "${MAX_ITERATIONS}"
  --use-encrypted-tasks "${USE_ENCRYPTED_TASKS}"
)

if [[ -n "${REQUEST_FILE}" ]]; then
  cmd+=(--request-file "${REQUEST_FILE}")
else
  cmd+=(--instance-id "${INSTANCE_ID}")
fi

if [[ -n "${UDA_TASKS_DIR}" ]]; then
  cmd+=(--uda-tasks-dir "${UDA_TASKS_DIR}")
fi

if [[ -n "${EC2_REGION}" ]]; then
  cmd+=(--ec2-region "${EC2_REGION}")
fi
if [[ -n "${EC2_AMI_ID}" ]]; then
  cmd+=(--ec2-ami-id "${EC2_AMI_ID}")
fi
if [[ -n "${EC2_LAUNCH_TEMPLATE_ID}" ]]; then
  cmd+=(--ec2-launch-template-id "${EC2_LAUNCH_TEMPLATE_ID}")
fi
if [[ -n "${EC2_LAUNCH_TEMPLATE_NAME}" ]]; then
  cmd+=(--ec2-launch-template-name "${EC2_LAUNCH_TEMPLATE_NAME}")
fi
if [[ -n "${EC2_LAUNCH_TEMPLATE_VERSION}" ]]; then
  cmd+=(--ec2-launch-template-version "${EC2_LAUNCH_TEMPLATE_VERSION}")
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
if [[ -n "${EC2_USE_PRIVATE_IP}" ]]; then
  cmd+=(--ec2-use-private-ip "${EC2_USE_PRIVATE_IP}")
fi
if [[ -n "${EC2_TERMINATE_ON_CLEANUP}" ]]; then
  cmd+=(--ec2-terminate-on-cleanup "${EC2_TERMINATE_ON_CLEANUP}")
fi
if [[ -n "${EC2_ENV_PROFILE}" ]]; then
  cmd+=(--ec2-env-profile "${EC2_ENV_PROFILE}")
fi

"${cmd[@]}"
