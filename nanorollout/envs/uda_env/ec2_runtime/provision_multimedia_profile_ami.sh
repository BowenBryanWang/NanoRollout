#!/usr/bin/env bash
# Add the multimedia profile layer on top of the UDA general-root AMI.
set -euo pipefail
exec > >(tee -a /var/log/uda-multimedia-profile-provision.log /dev/console) 2>&1
set -x

PROFILE_NAME="${UDA_PROFILE_NAME:-multimedia}"
MARKER_DIR="/opt/uda-ec2"
MARKER_JSON="${MARKER_DIR}/profile-${PROFILE_NAME}.json"
MARKER_READY="${MARKER_DIR}/profile-${PROFILE_NAME}.ready"
BLENDER_VERSION="${BLENDER_VERSION:-5.1.2}"
BLENDER_MAJOR_MINOR="${BLENDER_VERSION%.*}"
BLENDER_ARCHIVE="blender-${BLENDER_VERSION}-linux-x64.tar.xz"
BLENDER_DIR="/opt/blender/blender-${BLENDER_VERSION}-linux-x64"
BLENDER_URL="${BLENDER_URL:-https://download.blender.org/release/Blender${BLENDER_MAJOR_MINOR}/${BLENDER_ARCHIVE}}"

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends software-properties-common
add-apt-repository -y universe
add-apt-repository -y multiverse
apt-get update

APT_PACKAGES=(
  ca-certificates
  wget
  xz-utils
  blender
  kdenlive
  openshot-qt
  audacity
  handbrake
  handbrake-cli
  vlc
  obs-studio
  frei0r-plugins
  libxcb-cursor0
  libfuse2
  desktop-file-utils
)
AVAILABLE_PACKAGES=()
for pkg in "${APT_PACKAGES[@]}"; do
  if apt-cache show "${pkg}" >/dev/null 2>&1; then
    AVAILABLE_PACKAGES+=("${pkg}")
  else
    echo "WARNING: package not available in configured apt sources: ${pkg}" >&2
  fi
done
apt-get install -y --no-install-recommends "${AVAILABLE_PACKAGES[@]}"

install -d -m 0755 /opt/shotcut /usr/local/share/applications "${MARKER_DIR}"
install -d -m 0755 /opt/blender

if command -v blender >/dev/null 2>&1 && [[ -x /usr/bin/blender ]]; then
  ln -sf /usr/bin/blender /usr/local/bin/blender3
fi

wget -O "/opt/blender/${BLENDER_ARCHIVE}" "${BLENDER_URL}"
tar -C /opt/blender -xf "/opt/blender/${BLENDER_ARCHIVE}"
test -x "${BLENDER_DIR}/blender"
ln -sf "${BLENDER_DIR}/blender" /usr/local/bin/blender
blender --version
if command -v blender3 >/dev/null 2>&1; then
  blender3 --version || true
fi

python3 - <<'PY'
from __future__ import annotations

import json
import os
import stat
import urllib.request
from pathlib import Path

api = "https://api.github.com/repos/mltframework/shotcut/releases/latest"
target = Path("/opt/shotcut/shotcut.AppImage")
req = urllib.request.Request(api, headers={"User-Agent": "uda-ec2-ami-builder"})
with urllib.request.urlopen(req, timeout=30) as resp:
    release = json.loads(resp.read().decode("utf-8"))

assets = release.get("assets") or []
download = None
for asset in assets:
    name = asset.get("name", "")
    url = asset.get("browser_download_url")
    if "linux" in name.lower() and "x86_64" in name.lower() and name.endswith(".AppImage") and url:
        download = url
        break
if not download:
    raise RuntimeError("Could not find Shotcut Linux x86_64 AppImage in latest release")

with urllib.request.urlopen(download, timeout=300) as resp:
    target.write_bytes(resp.read())
target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
Path("/opt/shotcut/release.json").write_text(json.dumps({
    "tag_name": release.get("tag_name"),
    "asset_url": download,
}, indent=2), encoding="utf-8")
PY

cat >/usr/local/bin/shotcut <<'EOF'
#!/usr/bin/env bash
exec /opt/shotcut/shotcut.AppImage "$@"
EOF
chmod 0755 /usr/local/bin/shotcut

cat >/usr/local/share/applications/shotcut.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Shotcut
Exec=/usr/local/bin/shotcut
Icon=shotcut
Categories=AudioVideo;Video;AudioVideoEditing;
Terminal=false
EOF
update-desktop-database /usr/local/share/applications || true

python3 - <<'PY'
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

commands = {
    "blender": "blender --version",
    "blender3": "blender3 --version",
    "kdenlive": "kdenlive --version",
    "openshot": "openshot-qt --version",
    "audacity": "audacity --version",
    "handbrake": "HandBrakeCLI --version",
    "vlc": "vlc --version",
    "obs": "obs --version",
    "shotcut": "shotcut --version",
}

manifest = {
    "profile": "multimedia",
    "blender_default": {
        "version": "5.1.2",
        "path": "/usr/local/bin/blender",
        "legacy_path": "/usr/local/bin/blender3",
    },
    "includes": [
        "Blender 5.1.2 default on PATH as blender",
        "Ubuntu apt Blender retained as optional blender3 when available",
        "Kdenlive",
        "OpenShot",
        "Shotcut",
        "Audacity",
        "HandBrake",
        "VLC",
        "OBS Studio",
        "frei0r/mlt plugins",
    ],
    "commands": {},
}

for name, command in commands.items():
    binary = command.split()[0]
    path = shutil.which(binary)
    result = {"path": path}
    if path:
        try:
            env = dict(os.environ)
            env.setdefault("DISPLAY", ":0")
            env.setdefault("QT_QPA_PLATFORM", "offscreen")
            proc = subprocess.run(
                command,
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=20,
            )
            result["returncode"] = proc.returncode
            result["version_output"] = proc.stdout.splitlines()[:5]
        except Exception as exc:
            result["error"] = str(exc)
    manifest["commands"][name] = result

marker = Path("/opt/uda-ec2/profile-multimedia.json")
marker.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
Path("/opt/uda-ec2/profile-multimedia.ready").write_text("ok\n", encoding="utf-8")
PY

systemctl restart uda-compat.service || true
echo "UDA multimedia profile installed"
