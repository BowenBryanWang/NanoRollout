#!/usr/bin/env python3
"""Small /v1 compatibility server for OSWorld-derived EC2 AMIs.

This is a bootstrap bridge, not a full replacement for agent-infra's
python-server. It implements the endpoints NanoRollout currently uses
for UDA EC2 rollouts and maps computer-use actions to xdotool/scrot.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

HOST = os.environ.get("UDA_COMPAT_HOST", "0.0.0.0")
PORT = int(os.environ.get("UDA_COMPAT_PORT", "8080"))
HOME = os.environ.get("UDA_HOME", "/home/user")
WORKSPACE = os.environ.get("UDA_WORKSPACE", HOME)
DISPLAY = os.environ.get("DISPLAY", ":0")
SHELLS: dict[str, str] = {}


def ok(data=None, message="ok", **extra):
    body = {"success": True, "message": message, "data": data}
    body.update(extra)
    return body


def err(message, status=500):
    return status, {"success": False, "message": str(message), "data": None}


def run(cmd, cwd=None, timeout=600):
    env = dict(os.environ)
    env.setdefault("DISPLAY", DISPLAY)
    proc = subprocess.run(
        cmd,
        shell=True,
        executable="/bin/bash",
        cwd=cwd or WORKSPACE,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def b64_screenshot():
    out = Path(tempfile.gettempdir()) / f"uda-shot-{uuid.uuid4().hex}.png"
    pyautogui_cmd = (
        "python3 - <<'PY'\n"
        "import pyautogui\n"
        f"pyautogui.screenshot().save({str(out)!r})\n"
        "PY"
    )
    commands = [
        f"DISPLAY={shlex.quote(DISPLAY)} scrot -p {shlex.quote(str(out))}",
        f"DISPLAY={shlex.quote(DISPLAY)} gnome-screenshot -f {shlex.quote(str(out))} -p",
        f"DISPLAY={shlex.quote(DISPLAY)} {pyautogui_cmd}",
    ]
    for _ in range(6):
        for cmd in commands:
            if subprocess.call(cmd, shell=True) == 0 and out.exists() and out.stat().st_size > 0:
                data = base64.b64encode(out.read_bytes()).decode("ascii")
                out.unlink(missing_ok=True)
                return data
        time.sleep(1)
    return None


def ensure_parent(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def read_json(handler):
    n = int(handler.headers.get("content-length", "0") or "0")
    if n <= 0:
        return {}
    return json.loads(handler.rfile.read(n).decode("utf-8"))


def file_read(req):
    path = req.get("file") or req["path"]
    data = Path(path).read_text(errors="replace")
    start = req.get("start_line")
    end = req.get("end_line")
    if start is not None or end is not None:
        lines = data.splitlines(True)
        data = "".join(lines[int(start or 0): int(end) if end is not None else None])
    return ok({"content": data, "file": path})


def file_write(req):
    path = req.get("file") or req["path"]
    ensure_parent(path)
    mode = "ab" if req.get("append") else "wb"
    content = req.get("content", "")
    if req.get("encoding") == "base64":
        raw = base64.b64decode(content)
    else:
        raw = str(content).encode("utf-8")
    with open(path, mode) as fh:
        fh.write(raw)
    return ok({"file": path, "bytes_written": len(raw)})


def str_replace(req):
    path = req["path"]
    command = req["command"]
    p = Path(path)
    if command == "view":
        data = p.read_text(errors="replace") if p.exists() else ""
        return ok({"content": data, "file": path})
    if command == "create":
        ensure_parent(path)
        p.write_text(req.get("file_text", ""))
        return ok({"file": path})
    if command == "str_replace":
        data = p.read_text(errors="replace")
        old = req.get("old_str", "")
        new = req.get("new_str", "")
        if old not in data:
            raise ValueError("old_str not found")
        p.write_text(data.replace(old, new, 1))
        return ok({"file": path})
    if command == "insert":
        lines = p.read_text(errors="replace").splitlines(True)
        idx = int(req.get("insert_line", 0))
        lines.insert(idx, req.get("new_str", ""))
        p.write_text("".join(lines))
        return ok({"file": path})
    if command == "undo_edit":
        return ok({"file": path}, "undo_edit not implemented")
    raise ValueError(f"unknown editor command: {command}")


def shell_create(req):
    sid = req.get("id") or f"sh-{uuid.uuid4().hex}"
    wd = req.get("exec_dir") or WORKSPACE
    SHELLS[sid] = wd
    return ok({"session_id": sid, "working_dir": wd})


def shell_exec(req):
    sid = req.get("id") or f"sh-{uuid.uuid4().hex}"
    wd = req.get("exec_dir") or SHELLS.get(sid) or WORKSPACE
    SHELLS[sid] = wd
    rc, out, serr = run(req.get("command", ""), cwd=wd, timeout=int(req.get("timeout") or 600))
    output = out + (f"\n[stderr]\n{serr}" if serr else "")
    return ok({
        "session_id": sid,
        "command": req.get("command", ""),
        "status": "completed",
        "output": output,
        "exit_code": rc,
        "returncode": rc,
        "console": [],
    })


def code_execute(req):
    lang = str(req.get("language", "python")).lower()
    code = req.get("code", "")
    cwd = req.get("cwd") or WORKSPACE
    if lang != "python":
        return ok({"language": lang, "status": "error", "code": code, "stderr": "only python is implemented", "exit_code": 2})
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(code)
        script = fh.name
    try:
        rc, out, serr = run(f"python3 {shlex.quote(script)}", cwd=cwd, timeout=int(req.get("timeout") or 600))
    finally:
        Path(script).unlink(missing_ok=True)
    return ok({"language": "python", "status": "completed" if rc == 0 else "error", "code": code, "stdout": out, "stderr": serr, "exit_code": rc, "outputs": []})


def jupyter_create(req):
    sid = req.get("session_id") or f"jp-{uuid.uuid4().hex}"
    return ok({
        "session_id": sid,
        "kernel_name": req.get("kernel_name") or "python3",
        "message": "compat jupyter session created",
    })


def xdotool_cmd(req):
    action = req.get("action")
    xd = f"DISPLAY={shlex.quote(DISPLAY)} xdotool"
    coord = req.get("coordinate")
    if action == "screenshot":
        return {"output": None, "error": None, "base64_image": b64_screenshot()}
    if action == "cursor_position":
        rc, out, serr = run(f"{xd} getmouselocation --shell")
        return {"output": out, "error": serr if rc else None, "base64_image": None}
    parts = [xd]
    if coord and action in {"mouse_move", "left_click", "right_click", "middle_click", "double_click", "triple_click", "scroll"}:
        parts.append(f"mousemove --sync {int(coord[0])} {int(coord[1])}")
    if action == "mouse_move":
        pass
    elif action == "left_click":
        parts.append("click 1")
    elif action == "right_click":
        parts.append("click 3")
    elif action == "middle_click":
        parts.append("click 2")
    elif action == "double_click":
        parts.append("click --repeat 2 --delay 10 1")
    elif action == "triple_click":
        parts.append("click --repeat 3 --delay 10 1")
    elif action == "left_mouse_down":
        parts.append("mousedown 1")
    elif action == "left_mouse_up":
        parts.append("mouseup 1")
    elif action == "left_click_drag":
        s = req.get("start_coordinate") or [0, 0]
        e = req.get("coordinate") or [0, 0]
        parts.append(f"mousemove --sync {int(s[0])} {int(s[1])} mousedown 1 mousemove --sync {int(e[0])} {int(e[1])} mouseup 1")
    elif action == "key":
        parts.append(f"key -- {shlex.quote(req.get('text') or req.get('key') or '')}")
    elif action == "type":
        parts.append(f"type --delay 12 -- {shlex.quote(req.get('text') or '')}")
    elif action == "hold_key":
        key = shlex.quote(req.get("text") or req.get("key") or "")
        parts.append(f"keydown {key}; sleep {float(req.get('duration') or 0)}; {xd} keyup {key}")
    elif action == "scroll":
        buttons = {"up": 4, "down": 5, "left": 6, "right": 7}
        parts.append(f"click --repeat {int(req.get('scroll_amount') or 0)} {buttons.get(req.get('scroll_direction'), 5)}")
    elif action == "wait":
        time.sleep(float(req.get("duration") or 1))
        return {"output": None, "error": None, "base64_image": b64_screenshot()}
    elif action == "zoom":
        return {"output": None, "error": None, "base64_image": b64_screenshot()}
    else:
        return {"output": None, "error": f"unsupported action {action}", "base64_image": None}
    rc, out, serr = run(" ".join(parts))
    time.sleep(0.25)
    return {"output": out or None, "error": serr if rc else None, "base64_image": b64_screenshot()}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])
        if path == "/v1/sandbox":
            self._send(ok(None, home_dir=HOME, workspace=WORKSPACE, version="uda-ec2-compat-0.1", detail={"runtime": "ec2"}))
        elif path == "/v1/code/info":
            self._send(ok({"languages": ["python"]}))
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = unquote(self.path.split("?", 1)[0])
        try:
            req = read_json(self)
            routes = {
                "/v1/file/read": file_read,
                "/v1/file/write": file_write,
                "/v1/file/str_replace_editor": str_replace,
                "/v1/shell/create": shell_create,
                "/v1/shell/sessions/create": shell_create,
                "/v1/shell/exec": shell_exec,
                "/v1/code/execute": code_execute,
                "/v1/jupyter/sessions/create": jupyter_create,
            }
            if path == "/v1/computer-use/action":
                self._send(xdotool_cmd(req))
            elif path in routes:
                self._send(routes[path](req))
            else:
                self._send({"error": "not found"}, 404)
        except Exception as exc:
            status, body = err(exc)
            self._send(body, status)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
