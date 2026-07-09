#!/usr/bin/env python3
"""Smoke test a UDA EC2 runtime base URL."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from urllib import request


DIRECT_OPENER = request.build_opener(request.ProxyHandler({}))


def json_request(method, base, path, payload=None, timeout=60):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(
        f"{base.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with DIRECT_OPENER.open(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def post(base, path, payload, timeout=60):
    _, body = json_request("POST", base, path, payload, timeout)
    return body


def main():
    p = argparse.ArgumentParser()
    p.add_argument("base_url", help="Example: http://1.2.3.4:8080")
    p.add_argument("--screenshot-out", default=None)
    args = p.parse_args()
    base = args.base_url.rstrip("/")

    checks = {}

    sandbox_status, sandbox_body = json_request("GET", base, "/v1/sandbox", timeout=30)
    checks["sandbox"] = sandbox_body

    code_info_status, code_info_body = json_request("GET", base, "/v1/code/info", timeout=30)
    checks["code_info"] = code_info_body

    shell = post(base, "/v1/shell/create", {"exec_dir": "/home/user"})
    sid = shell["data"]["session_id"]
    checks["shell_create"] = shell
    shell2 = post(base, "/v1/shell/sessions/create", {"exec_dir": "/home/user"})
    sid2 = shell2["data"]["session_id"]
    checks["shell_sessions_create"] = shell2
    checks["shell_exec"] = post(
        base,
        "/v1/shell/exec",
        {"id": sid, "exec_dir": "/home/user", "command": "whoami && pwd && echo uda-smoke"},
    )
    checks["shell_sessions_exec"] = post(
        base,
        "/v1/shell/exec",
        {"id": sid2, "exec_dir": "/tmp", "command": "pwd && echo uda-smoke-session"},
    )

    checks["file_write"] = post(
        base,
        "/v1/file/write",
        {"file": "/tmp/uda-smoke.txt", "content": "hello ec2\n"},
    )
    checks["file_read"] = post(
        base,
        "/v1/file/read",
        {"file": "/tmp/uda-smoke.txt"},
    )

    checks["editor_create"] = post(
        base,
        "/v1/file/str_replace_editor",
        {"command": "create", "path": "/tmp/uda-editor-smoke.txt", "file_text": "alpha\nomega\n"},
    )
    checks["editor_view_initial"] = post(
        base,
        "/v1/file/str_replace_editor",
        {"command": "view", "path": "/tmp/uda-editor-smoke.txt"},
    )
    checks["editor_replace"] = post(
        base,
        "/v1/file/str_replace_editor",
        {"command": "str_replace", "path": "/tmp/uda-editor-smoke.txt", "old_str": "omega", "new_str": "beta"},
    )
    checks["editor_insert"] = post(
        base,
        "/v1/file/str_replace_editor",
        {"command": "insert", "path": "/tmp/uda-editor-smoke.txt", "insert_line": 1, "new_str": "inserted\n"},
    )
    checks["editor_view_final"] = post(
        base,
        "/v1/file/str_replace_editor",
        {"command": "view", "path": "/tmp/uda-editor-smoke.txt"},
    )

    checks["code"] = post(
        base,
        "/v1/code/execute",
        {"language": "python", "code": "print(6 * 7)"},
    )

    checks["jupyter_session"] = post(
        base,
        "/v1/jupyter/sessions/create",
        {"kernel_name": "python3"},
    )

    shot = post(base, "/v1/computer-use/action", {"action": "screenshot"}, timeout=90)
    b64 = shot.get("base64_image")
    checks["screenshot"] = {
        "has_image": bool(b64),
        "base64_bytes": len(b64 or ""),
        "error": shot.get("error"),
    }
    if b64:
        # Validate the returned payload is at least decodable.
        raw = base64.b64decode(b64)
        if args.screenshot_out:
            out = Path(args.screenshot_out).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)

    checks["cursor_position"] = post(
        base,
        "/v1/computer-use/action",
        {"action": "cursor_position"},
        timeout=30,
    )

    print(json.dumps(checks, indent=2)[:12000])

    failures = []
    if sandbox_status != 200:
        failures.append("sandbox")
    if code_info_status != 200 or "python" not in json.dumps(checks["code_info"]).lower():
        failures.append("code_info")
    if "uda-smoke" not in (checks["shell_exec"].get("data", {}).get("output") or ""):
        failures.append("shell")
    if "uda-smoke-session" not in (
        checks["shell_sessions_exec"].get("data", {}).get("output") or ""
    ):
        failures.append("shell_sessions")
    if "hello ec2" not in (checks["file_read"].get("data", {}).get("content") or ""):
        failures.append("file")
    final_editor = checks["editor_view_final"].get("data", {}).get("content") or ""
    if "alpha\ninserted\nbeta" not in final_editor:
        failures.append("str_replace_editor")
    if "42" not in (checks["code"].get("data", {}).get("stdout") or ""):
        failures.append("code")
    if not checks["jupyter_session"].get("data", {}).get("session_id"):
        failures.append("jupyter")
    if not checks["screenshot"]["has_image"]:
        failures.append("screenshot")
    if checks["cursor_position"].get("error"):
        failures.append("cursor_position")

    if failures:
        print(f"FAILED: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("UDA EC2 smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
