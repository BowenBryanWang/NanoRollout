#!/usr/bin/env bash
set -euo pipefail

mkdir -p /tmp_workspace/results
test -f /tmp_workspace/context/exports/run_metrics_export.csv
test -f /tmp_workspace/context/exports/qa_drift_checks.csv
test -f /tmp_workspace/context/exports/candidate_manifest.json
test -f /tmp_workspace/.uda_hidden/cua_mock/wandb_mock_initial_state.json
test -f /tmp_workspace/.uda_hidden/cua_mock/mock_site_name_table.json

python3 - <<'PY'
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

mock_name = "wandb_mock"
name_table_path = Path("/tmp_workspace/.uda_hidden/cua_mock/mock_site_name_table.json")
name_table = json.loads(name_table_path.read_text(encoding="utf-8"))
mock_entry = name_table["mocks"][mock_name]
base_url = mock_entry["base_url"].rstrip("/")
public_host = mock_entry["public_host"]
admin_token = os.environ.get("CUA_GYM_ADMIN_TOKEN", "").strip()
if not admin_token:
    raise RuntimeError("CUA_GYM_ADMIN_TOKEN must be set in the harness environment")
sid = "codex100-002-" + str(uuid.uuid4())
state_path = Path("/tmp_workspace/.uda_hidden/cua_mock/wandb_mock_initial_state.json")
state = json.loads(state_path.read_text(encoding="utf-8"))
runtime_dir = Path("/tmp/.uda_gym_runtime/codex100_002")
runtime_dir.mkdir(parents=True, exist_ok=True)
sid_path = runtime_dir / "wandb_sid"
sid_path.write_text(sid, encoding="utf-8")
os.chmod(sid_path, 0o600)
runtime_config_path = runtime_dir / "mock_site_runtime.json"
runtime_config_path.write_text(
    json.dumps(
        {
            "base_url": base_url,
            "public_host": public_host,
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
os.chmod(runtime_config_path, 0o600)

def admin_headers(content_type=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    if content_type:
        headers["Content-Type"] = "application/json"
    headers["X-CUA-Admin-Token"] = admin_token
    return headers

def curl_json(url, payload=None):
    args = [
        "curl",
        "-sS",
        "--fail-with-body",
        "-H",
        "User-Agent: Mozilla/5.0",
        "-H",
        f"X-CUA-Admin-Token: {admin_token}",
    ]
    tmp_path = None
    if payload is not None:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        args.extend(["-H", "Content-Type: application/json", "--data-binary", f"@{tmp_path}"])
    args.append(url)
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=30)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"curl failed for {url}: rc={result.returncode} stderr={result.stderr!r} stdout={result.stdout[:500]!r}"
        )
    body = result.stdout
    return json.loads(body) if body.strip().startswith("{") else {}

post_result = curl_json(f"{base_url}/post?sid={sid}", {"action": "set", "state": state})
go = curl_json(f"{base_url}/go?sid={sid}")
if not go.get("initial_state") or not go.get("current_state"):
    raise RuntimeError("W&B mock state injection failed")

launch_url = post_result.get("launch_url")
if not launch_url:
    raise RuntimeError("W&B mock did not return a hardened launch_url; deploy the site with CUA_GYM_HARDENED=1 before running this bundle")
agent_url = f"{base_url}{launch_url}"
env = os.environ.copy()
env.setdefault("DISPLAY", ":0")
for binary in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
    if shutil.which(binary):
        subprocess.Popen(
            [
                binary,
                "--no-sandbox",
                "--disable-dev-shm-usage",
                agent_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        time.sleep(3)
        print("GUI_READY: opened seeded W&B dashboard in Chrome")
        break
else:
    print("GUI_READY_WARNING: no Chrome binary found for seeded W&B dashboard")
PY
