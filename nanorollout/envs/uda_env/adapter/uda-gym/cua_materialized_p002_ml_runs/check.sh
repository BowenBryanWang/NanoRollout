#!/usr/bin/env bash
# UDA-Gym evaluator for uda_20260629_p002_ml_runs.
# Read-only W&B dashboard inspection task. Scores the two structured local
# deliverables (run_evaluation.csv + deployment_decision.json) against the gt/
# answer key, and confirms via admin-token /go?sid=<sid> that this read-only
# inspection left no meaningful state diff on the seeded wandb_mock.
#
# Reward truth for deliverables: /tmp_workspace/results/*.
# Answer key: /tmp_workspace/gt/expected_runs.json.
# Live state source of truth: admin-token GET https://cua-gym-wandb.xlang.ai/go?sid=<sid>.
set -euo pipefail

# Env-overridable paths so the script can be self-tested outside the VM.
RESULTS_DIR="${RESULTS_DIR:-/tmp_workspace/results}"
GT_DIR="${GT_DIR:-/tmp_workspace/gt}"
TASK_ID="uda_20260629_p002_ml_runs"
MOCK="wandb_mock"
BASE_URL="${BASE_URL:-https://cua-gym-wandb.xlang.ai}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/.uda_gym_runtime/${TASK_ID}}"
SID_FILE="${SID_FILE:-${RUNTIME_DIR}/${MOCK}_sid}"

CSV_FILE="${RESULTS_DIR}/run_evaluation.csv"
JSON_FILE="${RESULTS_DIR}/deployment_decision.json"
GT_FILE="${GT_DIR}/expected_runs.json"

# Resolve admin token (harness-only). Optional: the state-diff subscore is
# skipped (scored 0, no crash) if the token / sid are unavailable.
ADMIN_TOKEN="${CUA_GYM_ADMIN_TOKEN:-}"
if [ -z "${ADMIN_TOKEN}" ] && [ -f /home/ubuntu/.cua-gym-hub-admin-token ]; then
  ADMIN_TOKEN="$(cat /home/ubuntu/.cua-gym-hub-admin-token 2>/dev/null || true)"
fi

SID=""
if [ -f "${SID_FILE}" ]; then
  SID="$(cat "${SID_FILE}" 2>/dev/null || true)"
fi

python3 - "$CSV_FILE" "$JSON_FILE" "$GT_FILE" "$BASE_URL" "$SID" "$ADMIN_TOKEN" <<'PY'
import csv, json, sys, urllib.request

csv_path, json_path, gt_path, base_url, sid, token = sys.argv[1:7]

errors = []
subscores = {}

# --- Weights (sum to 1.0) ---
W_CSV_STRUCT   = 0.10   # header exact + 10 data rows
W_CSV_DEPLOY   = 0.20   # per-run deployable flag (10 runs)
W_CSV_REASON   = 0.20   # per-run exclusion_reason (10 runs)
W_CSV_BLANKS   = 0.10   # blank latency/cost for run-7/run-8, populated for finished
W_JSON_RECO    = 0.15   # recommended_* fields
W_JSON_DEPLOY  = 0.10   # deployable_run_ids + deployable_count
W_JSON_EXCL    = 0.10   # excluded map exact
W_STATE_DIFF   = 0.05   # read-only: no meaningful state diff

# --- Load answer key ---
try:
    with open(gt_path) as f:
        gt = json.load(f)
except Exception as e:
    print(json.dumps({"overall_score": 0.0, "subscores": {},
                      "errors": [f"cannot load gt answer key: {e}"]}))
    sys.exit(0)

gt_runs = {r["run_id"]: r for r in gt["runs"]}
gt_ids = [r["run_id"] for r in gt["runs"]]
gt_dec = gt["expected_decision"]
never_finished = {"run-7", "run-8"}   # blank p95/cost
finished_ids = [rid for rid in gt_ids if rid not in never_finished]

EXPECTED_HEADER = ["run_id", "run_name", "model", "state", "f1", "em",
                   "p95_latency_ms", "cost_per_1k_usd", "eval_set", "eval_n",
                   "deployable", "exclusion_reason"]

def num_eq(a, b, tol=1e-6):
    try:
        return abs(float(a) - float(b)) <= tol
    except Exception:
        return False

def norm_bool(v):
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None

# ------------------------------------------------------------------ CSV ----
csv_rows = None
header = None
try:
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        all_rows = [row for row in reader if row != []]
    if all_rows:
        header = [c.strip() for c in all_rows[0]]
        csv_rows = all_rows[1:]
except Exception as e:
    errors.append(f"cannot read run_evaluation.csv: {e}")

# Build dict form keyed by run_id for tolerant per-field checks.
csv_by_id = {}
if csv_rows is not None and header is not None:
    for row in csv_rows:
        d = {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
        rid = str(d.get("run_id", "")).strip()
        if rid:
            csv_by_id[rid] = d

# --- CSV structure: header exact + exactly 10 data rows ---
struct = 0.0
if header is not None:
    if header == EXPECTED_HEADER:
        struct += 0.5
    else:
        errors.append(f"CSV header mismatch: got {header}")
    if csv_rows is not None and len(csv_rows) == 10:
        struct += 0.5
    else:
        n = len(csv_rows) if csv_rows is not None else 0
        errors.append(f"CSV expected 10 data rows, got {n}")
subscores["csv_structure"] = round(W_CSV_STRUCT * struct, 6)

# --- CSV per-run deployable flag ---
dep_ok = 0
for rid in gt_ids:
    d = csv_by_id.get(rid)
    if not d:
        continue
    got = norm_bool(d.get("deployable", ""))
    if got is not None and got == bool(gt_runs[rid]["deployable"]):
        dep_ok += 1
subscores["csv_deployable_flags"] = round(W_CSV_DEPLOY * dep_ok / len(gt_ids), 6)

# --- CSV per-run exclusion_reason (deployable rows must be empty) ---
reason_ok = 0
for rid in gt_ids:
    d = csv_by_id.get(rid)
    if not d:
        continue
    got = str(d.get("exclusion_reason", "")).strip()
    exp = str(gt_runs[rid]["exclusion_reason"]).strip()
    if got == exp:
        reason_ok += 1
subscores["csv_exclusion_reasons"] = round(W_CSV_REASON * reason_ok / len(gt_ids), 6)

# --- CSV blank latency/cost for never-finished; populated for finished ---
blank_checks = 0
blank_total = 0
# never-finished must be blank in both p95 and cost
for rid in never_finished:
    for col in ("p95_latency_ms", "cost_per_1k_usd"):
        blank_total += 1
        d = csv_by_id.get(rid)
        if d is not None and str(d.get(col, "")).strip() == "":
            blank_checks += 1
# finished must be populated with the correct numeric value
for rid in finished_ids:
    for col in ("p95_latency_ms", "cost_per_1k_usd"):
        blank_total += 1
        d = csv_by_id.get(rid)
        if d is not None:
            val = str(d.get(col, "")).strip()
            if val != "" and num_eq(val, gt_runs[rid][col]):
                blank_checks += 1
subscores["csv_blank_and_numeric"] = round(
    W_CSV_BLANKS * (blank_checks / blank_total if blank_total else 0), 6)

# ------------------------------------------------------------------ JSON ---
dec = None
try:
    with open(json_path) as f:
        dec = json.load(f)
except Exception as e:
    errors.append(f"cannot read deployment_decision.json: {e}")

# --- JSON recommended_* ---
reco = 0.0
if isinstance(dec, dict):
    fields = [
        ("recommended_run_id",   lambda a, b: str(a).strip() == str(b)),
        ("recommended_run_name", lambda a, b: str(a).strip() == str(b)),
        ("recommended_model",    lambda a, b: str(a).strip() == str(b)),
        ("recommended_f1",       num_eq),
    ]
    got_ok = 0
    for key, cmp in fields:
        if key in dec and cmp(dec[key], gt_dec[key]):
            got_ok += 1
    reco = got_ok / len(fields)
    if got_ok != len(fields):
        errors.append("deployment_decision.json recommended_* fields incomplete/incorrect")
subscores["json_recommended"] = round(W_JSON_RECO * reco, 6)

# --- JSON deployable_run_ids + deployable_count ---
depjson = 0.0
if isinstance(dec, dict):
    ids = dec.get("deployable_run_ids")
    if isinstance(ids, list) and [str(x).strip() for x in ids] == gt_dec["deployable_run_ids"]:
        depjson += 0.5
    else:
        errors.append("deployable_run_ids mismatch")
    if dec.get("deployable_count") == gt_dec["deployable_count"]:
        depjson += 0.5
    else:
        errors.append("deployable_count mismatch")
subscores["json_deployable"] = round(W_JSON_DEPLOY * depjson, 6)

# --- JSON excluded map exact (per reason code) ---
excl = 0.0
if isinstance(dec, dict):
    got_excl = dec.get("excluded", {})
    exp_excl = gt_dec["excluded"]
    codes = list(exp_excl.keys())
    matched = 0
    for code in codes:
        got_list = got_excl.get(code, []) if isinstance(got_excl, dict) else []
        got_norm = sorted(str(x).strip() for x in got_list) if isinstance(got_list, list) else None
        if got_norm == sorted(exp_excl[code]):
            matched += 1
    excl = matched / len(codes)
    # Penalize extra spurious reason keys mapping to non-empty lists.
    if isinstance(got_excl, dict):
        for k, v in got_excl.items():
            if k not in exp_excl and isinstance(v, list) and len(v) > 0:
                excl = max(0.0, excl - 1.0 / len(codes))
    if excl < 1.0:
        errors.append("excluded map does not exactly match expected reason->run_id mapping")
subscores["json_excluded_map"] = round(W_JSON_EXCL * excl, 6)

# ------------------------------------------------------- read-only state ---
# Authoritative live state via admin-token /go?sid=<sid>. This is a read-only
# task: confirm the seeded runs are intact and no meaningful mutation happened.
state_score = 0.0
if sid and token:
    try:
        req = urllib.request.Request(
            f"{base_url}/go?sid={sid}",
            headers={"X-CUA-Admin-Token": token},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        current = data.get("current_state") or data.get("initial_state") or {}
        runs = current.get("runs") or []
        projects = current.get("projects") or []
        proj_ok = bool(projects) and projects[0].get("name") == "qa-eval"
        runs_ok = len(runs) == 10
        # No destructive diff: still 10 runs in qa-eval.
        if proj_ok and runs_ok:
            state_score = 1.0
        else:
            errors.append(
                f"live state check: expected 10 runs in qa-eval, got runs={len(runs)} proj_ok={proj_ok}")
    except Exception as e:
        errors.append(f"live /go state check failed (non-fatal): {e}")
else:
    errors.append("sid or admin token unavailable; skipping live state-diff subscore")
subscores["readonly_state_intact"] = round(W_STATE_DIFF * state_score, 6)

overall = round(sum(subscores.values()), 6)
if overall > 1.0:
    overall = 1.0

print(json.dumps({
    "overall_score": overall,
    "subscores": subscores,
    "errors": errors,
}))
PY
