#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
import csv
import json
import math
import os
import subprocess
import urllib.request
from pathlib import Path

BASE = Path("/tmp_workspace")
RESULT = BASE / "results" / "model_selection.json"
EXPORTS = BASE / "context" / "exports"
SID_FILE = Path("/tmp/.uda_gym_runtime/codex100_002/wandb_sid")
RUNTIME_CONFIG = Path("/tmp/.uda_gym_runtime/codex100_002/mock_site_runtime.json")
WAND_URL = "https://cua-gym-wandb.xlang.ai"
ADMIN_TOKEN = os.environ.get("CUA_GYM_ADMIN_TOKEN", "").strip()

ALLOWED_REASONS = {
    "not_finished",
    "qa_drift_fail",
    "split_health_fail",
    "metric_mismatch",
    "not_candidate",
}

def score(payload):
    print(json.dumps(payload, sort_keys=True))

def admin_headers():
    if not ADMIN_TOKEN:
        raise RuntimeError("CUA_GYM_ADMIN_TOKEN must be set in the harness environment")
    headers = {"User-Agent": "Mozilla/5.0"}
    headers["X-CUA-Admin-Token"] = ADMIN_TOKEN
    return headers

def curl_json(url):
    if not ADMIN_TOKEN:
        raise RuntimeError("CUA_GYM_ADMIN_TOKEN must be set in the harness environment")
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "--fail-with-body",
            "-H",
            "User-Agent: Mozilla/5.0",
            "-H",
            f"X-CUA-Admin-Token: {ADMIN_TOKEN}",
            url,
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"curl failed for {url}: rc={result.returncode} stderr={result.stderr!r} stdout={result.stdout[:500]!r}"
        )
    return json.loads(result.stdout)

try:
    sid = SID_FILE.read_text(encoding="utf-8").strip()
    runtime_config = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    web = curl_json(
        f"{runtime_config['base_url']}/go?sid={sid}",
    )
    current = web["current_state"]
    runs = {run["runId"]: run for run in current["runs"]}

    manifest = json.loads((EXPORTS / "candidate_manifest.json").read_text(encoding="utf-8"))
    project = manifest["project"]
    baseline_id = manifest["baseline_run_id"]
    candidate_ids = list(manifest["candidate_run_ids"])
    rules = manifest["eligibility_rules"]

    with (EXPORTS / "run_metrics_export.csv").open(encoding="utf-8", newline="") as f:
        metrics = {row["run_id"]: row for row in csv.DictReader(f)}
    with (EXPORTS / "qa_drift_checks.csv").open(encoding="utf-8", newline="") as f:
        qa = {row["run_id"]: row for row in csv.DictReader(f)}

    expected_excluded = []
    eligible = []
    for run_id in candidate_ids:
        run = runs.get(run_id)
        if run is None:
            expected_excluded.append({"run_id": run_id, "reason_code": "metric_mismatch"})
            continue
        if run.get("state") not in rules["allowed_dashboard_states"]:
            expected_excluded.append({"run_id": run_id, "reason_code": "not_finished"})
            continue
        q = qa.get(run_id)
        if q is None:
            expected_excluded.append({"run_id": run_id, "reason_code": "metric_mismatch"})
            continue
        if float(q["split_leakage_pct"]) > float(rules["max_split_leakage_pct"]):
            expected_excluded.append({"run_id": run_id, "reason_code": "split_health_fail"})
            continue
        if q["qa_status"] != "pass" or float(q["psi_max"]) > float(rules["max_psi"]) or float(q["null_rate_pct"]) > float(rules["max_null_rate_pct"]):
            expected_excluded.append({"run_id": run_id, "reason_code": "qa_drift_fail"})
            continue
        metric = metrics.get(run_id)
        if metric is None:
            expected_excluded.append({"run_id": run_id, "reason_code": "metric_mismatch"})
            continue
        eligible.append(run_id)

    eligible.sort(key=lambda rid: (-float(metrics[rid]["val_f1"]), rid))
    selected_id = eligible[0] if eligible else None
    expected_delta_f1 = round(float(metrics[selected_id]["val_f1"]) - float(metrics[baseline_id]["val_f1"]), 3)
    expected_delta_latency = round(float(metrics[selected_id]["latency_ms"]) - float(metrics[baseline_id]["latency_ms"]), 3)

    if not RESULT.exists():
        score({"overall_score": 0.0, "error": "missing /tmp_workspace/results/model_selection.json"})
        raise SystemExit(0)
    actual = json.loads(RESULT.read_text(encoding="utf-8"))

    excluded_actual = sorted(actual.get("excluded_runs", []), key=lambda item: item.get("run_id", ""))
    excluded_expected = sorted(expected_excluded, key=lambda item: item["run_id"])
    deltas = actual.get("metric_deltas", {})
    reason_codes = {item.get("reason_code") for item in actual.get("excluded_runs", []) if isinstance(item, dict)}

    checks = {
        "project": 1.0 if actual.get("project") == project else 0.0,
        "baseline_run_id": 1.0 if actual.get("baseline_run_id") == baseline_id else 0.0,
        "selected_run_id": 1.0 if actual.get("selected_run_id") == selected_id else 0.0,
        "eligible_run_ids": 1.0 if actual.get("eligible_run_ids") == eligible else 0.0,
        "excluded_runs": 1.0 if excluded_actual == excluded_expected else 0.0,
        "reason_codes_allowed": 1.0 if reason_codes <= ALLOWED_REASONS else 0.0,
        "delta_f1": 1.0 if math.isclose(float(deltas.get("selected_vs_baseline_f1", 999)), expected_delta_f1, abs_tol=0.0005) else 0.0,
        "delta_latency": 1.0 if math.isclose(float(deltas.get("selected_vs_baseline_latency_ms", 999)), expected_delta_latency, abs_tol=0.5) else 0.0,
        "no_freeform_prose": 1.0 if not any(k in actual for k in ("summary", "notes", "explanation", "report")) else 0.0,
        "web_state_readback": 1.0 if runs.get("run-beta-09", {}).get("state") != "finished" and runs.get("run-zeta-03", {}).get("state") != "finished" else 0.0,
    }
    checks["overall_score"] = round(sum(checks.values()) / len(checks), 4)
    score(checks)
except Exception as exc:
    score({"overall_score": 0.0, "error": str(exc)})
PY
