#!/usr/bin/env python3
"""Smoke test GUI behavior through the UDA /v1 computer-use surface."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from urllib import request


DIRECT_OPENER = request.build_opener(request.ProxyHandler({}))


def json_request(method: str, base: str, path: str, payload=None, timeout=120):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(f"{base.rstrip('/')}{path}", data=data, headers=headers, method=method)
    with DIRECT_OPENER.open(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if body else {}


def shell(base: str, command: str, timeout=120):
    return json_request(
        "POST",
        base,
        "/v1/shell/exec",
        {"exec_dir": "/home/user", "command": command, "timeout": timeout},
        timeout=timeout + 20,
    )


def computer(base: str, payload: dict, timeout=120):
    return json_request("POST", base, "/v1/computer-use/action", payload, timeout=timeout)


def save_screenshot(base: str, out: Path, label: str):
    shot = computer(base, {"action": "screenshot"}, timeout=120)
    b64 = shot.get("base64_image") or ""
    info = {"label": label, "base64_bytes": len(b64), "error": shot.get("error")}
    if b64:
        raw = base64.b64decode(b64)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
        info["path"] = str(out)
        info["bytes"] = len(raw)
    return info


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("base_url", help="Example: http://1.2.3.4:8080")
    p.add_argument("--screenshot-dir", default="/tmp/uda-ec2-gui-smoke")
    p.add_argument("--skip-chrome", action="store_true")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    out_dir = Path(args.screenshot_dir).expanduser()
    checks: dict[str, object] = {}
    failures: list[str] = []

    checks["display_probe"] = shell(
        base,
        r"""
set -e
echo "USER=$(id -un)"
echo "DISPLAY=${DISPLAY:-}"
for d in "${DISPLAY:-:0}" :0 :1 :2; do
  echo "DISPLAY_PROBE=$d"
  DISPLAY=$d xdpyinfo 2>&1 | awk '/dimensions:/{print; ok=1} END{if(!ok) print "no-x"}'
done
command -v xmessage || true
command -v libreoffice || true
command -v google-chrome || true
command -v xdotool || true
command -v scrot || true
""",
    )
    display_out = checks["display_probe"].get("data", {}).get("output", "")  # type: ignore[index]
    if "dimensions:" not in display_out:
        failures.append("display")

    checks["launch_xmessage"] = shell(
        base,
        r"""
set -e
killall -q xmessage || true
nohup env DISPLAY=${DISPLAY:-:0} xmessage -geometry 760x240+120+120 'UDA EC2 GUI click smoke' >/tmp/uda-xmessage.log 2>&1 &
sleep 2
pgrep -a xmessage
""",
        timeout=40,
    )
    if "xmessage" not in checks["launch_xmessage"].get("data", {}).get("output", ""):  # type: ignore[index]
        failures.append("xmessage_launch")
    checks["xmessage_before_click"] = save_screenshot(
        base, out_dir / "xmessage-before-click.png", "xmessage_before_click"
    )
    if not checks["xmessage_before_click"].get("base64_bytes"):  # type: ignore[union-attr]
        failures.append("xmessage_screenshot")
    checks["xmessage_click"] = computer(base, {"action": "left_click", "coordinate": [143, 384]})
    checks["xmessage_after_click"] = shell(base, "sleep 1; pgrep -a xmessage || true", timeout=30)
    if "xmessage" in checks["xmessage_after_click"].get("data", {}).get("output", ""):  # type: ignore[index]
        failures.append("click")

    checks["launch_libreoffice"] = shell(
        base,
        r"""
set -e
killall -q libreoffice soffice.bin soffice || true
nohup env DISPLAY=${DISPLAY:-:0} HOME=${HOME:-/home/user} libreoffice --writer --nologo --nofirststartwizard --norestore >/tmp/uda-libreoffice.log 2>&1 &
sleep 20
pgrep -a -f 'libreoffice|soffice.bin|soffice'
tail -80 /tmp/uda-libreoffice.log || true
""",
        timeout=80,
    )
    if "soffice" not in checks["launch_libreoffice"].get("data", {}).get("output", ""):  # type: ignore[index]
        failures.append("libreoffice_launch")
    checks["libreoffice_screenshot"] = save_screenshot(
        base, out_dir / "libreoffice-writer.png", "libreoffice"
    )
    if not checks["libreoffice_screenshot"].get("base64_bytes"):  # type: ignore[union-attr]
        failures.append("libreoffice_screenshot")

    if not args.skip_chrome:
        checks["launch_chrome"] = shell(
            base,
            r"""
set -e
killall -q chrome chrome_crashpad_handler google-chrome || true
rm -rf /tmp/uda-chrome-profile
nohup env DISPLAY=${DISPLAY:-:0} HOME=${HOME:-/home/user} google-chrome --no-sandbox --disable-dev-shm-usage --disable-gpu --user-data-dir=/tmp/uda-chrome-profile about:blank >/tmp/uda-chrome.log 2>&1 &
sleep 18
pgrep -a -f 'chrome|google-chrome'
tail -80 /tmp/uda-chrome.log || true
""",
            timeout=90,
        )
        if "chrome" not in checks["launch_chrome"].get("data", {}).get("output", ""):  # type: ignore[index]
            failures.append("chrome_launch")
        checks["chrome_screenshot"] = save_screenshot(base, out_dir / "chrome.png", "chrome")
        if not checks["chrome_screenshot"].get("base64_bytes"):  # type: ignore[union-attr]
            failures.append("chrome_screenshot")

    checks["cleanup"] = shell(
        base,
        "killall -q xmessage libreoffice soffice.bin soffice chrome chrome_crashpad_handler google-chrome || true",
        timeout=40,
    )

    result = {"checks": checks, "failures": failures}
    print(json.dumps(result, indent=2, default=str)[:20000])
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("UDA EC2 GUI smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
