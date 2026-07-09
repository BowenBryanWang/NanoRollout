#!/usr/bin/env bash
# Hidden pre-agent setup for uda_20260629_p002_ml_runs.
# Seeds the deployed wandb_mock (acme-nlp / qa-eval) with this task's state via the
# hardened-public-hybrid admin-token API, keeps the sid/token out of agent-visible
# paths, and opens Chrome to the one-time launch_url so the agent starts on the
# already-seeded dashboard.
set -euo pipefail

TASK_ID="uda_20260629_p002_ml_runs"
MOCK="wandb_mock"
BASE_URL="https://cua-gym-wandb.xlang.ai"

# Hidden staging (copied to /tmp_workspace/.uda_hidden before this script runs).
HIDDEN_DIR="/tmp_workspace/.uda_hidden"
STATE_FILE="${HIDDEN_DIR}/cua_mock/wandb_mock_initial_state.json"

# Harness-only runtime dir for the real sid. Never under /tmp_workspace.
RUNTIME_DIR="/tmp/.uda_gym_runtime/${TASK_ID}"
SID_FILE="${RUNTIME_DIR}/${MOCK}_sid"

# --- Agent-visible deliverable dir ---
mkdir -p /tmp_workspace/results

# --- Resolve admin token (harness-only) ---
ADMIN_TOKEN="${CUA_GYM_ADMIN_TOKEN:-}"
if [ -z "${ADMIN_TOKEN}" ] && [ -f /home/ubuntu/.cua-gym-hub-admin-token ]; then
  ADMIN_TOKEN="$(cat /home/ubuntu/.cua-gym-hub-admin-token)"
fi
if [ -z "${ADMIN_TOKEN}" ]; then
  echo "FATAL: CUA_GYM_ADMIN_TOKEN not available for hardened wandb_mock seeding" >&2
  exit 1
fi

# --- Verify the hidden seed state is staged ---
if [ ! -f "${STATE_FILE}" ]; then
  echo "FATAL: missing hidden seed state ${STATE_FILE}" >&2
  exit 1
fi

# --- Generate and persist a fresh sid in a harness-only path ---
mkdir -p "${RUNTIME_DIR}"
SID="${TASK_ID}-$(cat /proc/sys/kernel/random/uuid)"
printf '%s' "${SID}" > "${SID_FILE}"
chmod 600 "${SID_FILE}"

# --- Seed server-side state with admin-token POST /post?sid=<sid> ---
RESP="$(curl -fsS -X POST "${BASE_URL}/post?sid=${SID}" \
  -H 'Content-Type: application/json' \
  -H "X-CUA-Admin-Token: ${ADMIN_TOKEN}" \
  --data "$(printf '{"action":"set","state":%s}' "$(cat "${STATE_FILE}")")")"

LAUNCH_URL="$(printf '%s' "${RESP}" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("launch_url",""))')"
if [ -z "${LAUNCH_URL}" ]; then
  echo "FATAL: hardened launch_url missing from /post response: ${RESP}" >&2
  exit 1
fi

# --- Verify state landed (admin-token /go) ---
python3 - "$BASE_URL" "$SID" "$ADMIN_TOKEN" <<'PY'
import sys, json, urllib.request
base, sid, token = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(
    f"{base}/go?sid={sid}",
    headers={"X-CUA-Admin-Token": token},
)
with urllib.request.urlopen(req, timeout=15) as r:
    data = json.load(r)
init = data.get("initial_state") or {}
runs = init.get("runs") or []
projects = init.get("projects") or []
assert len(runs) == 10, f"expected 10 seeded runs, got {len(runs)}"
assert projects and projects[0].get("name") == "qa-eval", "qa-eval project not seeded"
print(f"seeded {MOCK}: 10 runs in qa-eval".replace("{MOCK}", "wandb_mock"))
PY

# --- Open Chrome on the one-time launch_url so the agent lands on the seeded dashboard ---
export DISPLAY="${DISPLAY:-:0}"
CHROME_LOG="/tmp/${TASK_ID}_chrome_launch.log"
nohup setsid google-chrome --no-sandbox --disable-dev-shm-usage \
  --window-size=1280,900 "${BASE_URL}${LAUNCH_URL}" \
  >"${CHROME_LOG}" 2>&1 &

for _ in $(seq 1 30); do
  if pgrep -u "$(id -u)" -f "[c]hrome.*cua-gym-wandb.xlang.ai" >/dev/null; then
    if command -v xdotool >/dev/null 2>&1; then
      if xdotool search --onlyvisible --class chrome >/dev/null 2>&1; then
        break
      fi
    else
      break
    fi
  fi
  sleep 0.5
done

if ! pgrep -u "$(id -u)" -f "[c]hrome.*cua-gym-wandb.xlang.ai" >/dev/null; then
  echo "FATAL: Chrome did not stay open on the seeded W&B dashboard" >&2
  sed -n '1,120p' "${CHROME_LOG}" >&2 || true
  exit 1
fi
if command -v xdotool >/dev/null 2>&1 && \
   ! xdotool search --onlyvisible --class chrome >/dev/null 2>&1; then
  echo "FATAL: Chrome process exists but no visible Chrome window was found" >&2
  sed -n '1,120p' "${CHROME_LOG}" >&2 || true
  exit 1
fi

echo "setup complete: seeded ${MOCK} and opened dashboard"
