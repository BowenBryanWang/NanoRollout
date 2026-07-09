#!/usr/bin/env bash
# Build the UDA general-base layer on a clean Ubuntu EC2 image.
set -euo pipefail
exec > >(tee -a /var/log/uda-root-provision.log /dev/console) 2>&1
set -x

UDA_USER="${UDA_USER:-user}"
UDA_HOME="${UDA_HOME:-/home/${UDA_USER}}"
UDA_WORKSPACE="${UDA_WORKSPACE:-${UDA_HOME}}"
UDA_TASK_WORKSPACE="${UDA_TASK_WORKSPACE:-/tmp_workspace}"
UDA_PORT="${UDA_PORT:-8080}"
UDA_DISPLAY="${UDA_DISPLAY:-:0}"
UDA_SCREEN="${UDA_SCREEN:-1920x1080x24}"
UDA_ENABLE_VNC="${UDA_ENABLE_VNC:-true}"
UDA_VNC_PORT="${UDA_VNC_PORT:-5910}"
UDA_ALLOW_PASSWORDLESS_SUDO="${UDA_ALLOW_PASSWORDLESS_SUDO:-false}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl wget gnupg
install -d -m 0755 /etc/apt/keyrings

if [[ "$(dpkg --print-architecture)" == "amd64" ]]; then
  wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /etc/apt/keyrings/google-linux.gpg
  chmod 0644 /etc/apt/keyrings/google-linux.gpg
  cat >/etc/apt/sources.list.d/google-chrome.list <<EOF
deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main
EOF

  wget -qO- https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /etc/apt/keyrings/packages.microsoft.gpg
  chmod 0644 /etc/apt/keyrings/packages.microsoft.gpg
  cat >/etc/apt/sources.list.d/vscode.list <<EOF
deb [arch=amd64 signed-by=/etc/apt/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main
EOF
else
  echo "Unsupported architecture for Chrome/VSCode base AMI: $(dpkg --print-architecture)" >&2
  exit 1
fi

if ! id "${UDA_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${UDA_USER}"
fi
install -d -o "${UDA_USER}" -g "${UDA_USER}" -m 0755 "${UDA_WORKSPACE}"
install -d -o "${UDA_USER}" -g "${UDA_USER}" -m 0755 \
  "${UDA_TASK_WORKSPACE}" "${UDA_TASK_WORKSPACE}/results"

if [[ "${UDA_ALLOW_PASSWORDLESS_SUDO}" == "true" ]]; then
  usermod -aG sudo "${UDA_USER}" || true
  cat >/etc/sudoers.d/90-uda-user <<EOF
${UDA_USER} ALL=(ALL) NOPASSWD:ALL
EOF
  chmod 0440 /etc/sudoers.d/90-uda-user
fi

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl wget gnupg jq git ripgrep unzip zip p7zip-full rsync tree \
  python3 python3-pip python3-venv python3-tk python3-dev \
  nodejs npm \
  build-essential pkg-config psmisc lsof \
  xvfb dbus-x11 openbox x11-xserver-utils x11-utils xauth \
  xdotool scrot gnome-screenshot imagemagick xclip xsel \
  x11vnc websockify novnc \
  google-chrome-stable code libreoffice evince gimp \
  poppler-utils qpdf ghostscript mupdf-tools \
  ffmpeg mediainfo libimage-exiftool-perl \
  fonts-noto fonts-noto-cjk fonts-noto-color-emoji fonts-liberation fonts-dejavu \
  python3-pil python3-xlib python3-pyperclip

install -d -m 0755 /opt/uda-ec2
install -m 0755 "${SCRIPT_DIR}/uda_compat_server.py" /opt/uda-ec2/uda_compat_server.py
chown -R root:root /opt/uda-ec2

cat >/etc/systemd/system/uda-xvfb.service <<EOF
[Unit]
Description=UDA virtual X display
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=-/usr/bin/fuser -k /tmp/.X0-lock
ExecStart=/usr/bin/Xvfb ${UDA_DISPLAY} -screen 0 ${UDA_SCREEN} -ac +extension GLX +render -noreset
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/uda-desktop.service <<EOF
[Unit]
Description=UDA lightweight desktop session
After=uda-xvfb.service
Requires=uda-xvfb.service

[Service]
Type=simple
User=${UDA_USER}
Environment=DISPLAY=${UDA_DISPLAY}
Environment=HOME=${UDA_HOME}
WorkingDirectory=${UDA_WORKSPACE}
ExecStart=/usr/bin/openbox-session
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

if [[ "${UDA_ENABLE_VNC}" == "true" ]]; then
  cat >/etc/systemd/system/uda-x11vnc.service <<EOF
[Unit]
Description=UDA x11vnc desktop access
After=uda-xvfb.service uda-desktop.service
Requires=uda-xvfb.service

[Service]
Type=simple
Environment=DISPLAY=${UDA_DISPLAY}
ExecStart=/usr/bin/x11vnc -display ${UDA_DISPLAY} -forever -shared -nopw -listen 0.0.0.0 -rfbport ${UDA_VNC_PORT}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
fi

cat >/etc/systemd/system/uda-compat.service <<EOF
[Unit]
Description=UDA EC2 /v1 compatibility API
After=network-online.target uda-xvfb.service uda-desktop.service
Wants=network-online.target
Requires=uda-xvfb.service

[Service]
Type=simple
User=${UDA_USER}
Environment=DISPLAY=${UDA_DISPLAY}
Environment=HOME=${UDA_HOME}
Environment=UDA_HOME=${UDA_HOME}
Environment=UDA_WORKSPACE=${UDA_WORKSPACE}
Environment=UDA_COMPAT_HOST=0.0.0.0
Environment=UDA_COMPAT_PORT=${UDA_PORT}
WorkingDirectory=${UDA_WORKSPACE}
ExecStartPre=-/usr/bin/fuser -k ${UDA_PORT}/tcp
ExecStart=/usr/bin/python3 /opt/uda-ec2/uda_compat_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable uda-xvfb.service uda-desktop.service uda-compat.service
if [[ "${UDA_ENABLE_VNC}" == "true" ]]; then
  systemctl enable uda-x11vnc.service
fi
systemctl restart uda-xvfb.service
sleep 2
systemctl restart uda-desktop.service
if [[ "${UDA_ENABLE_VNC}" == "true" ]]; then
  systemctl restart uda-x11vnc.service || true
fi
systemctl restart uda-compat.service

if command -v ufw >/dev/null 2>&1 && ufw status | grep -qi active; then
  ufw allow "${UDA_PORT}/tcp" || true
  if [[ "${UDA_ENABLE_VNC}" == "true" ]]; then
    ufw allow "${UDA_VNC_PORT}/tcp" || true
  fi
fi

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${UDA_PORT}/v1/sandbox" >/tmp/uda-compat-health.json; then
    echo "UDA Ubuntu root AMI layer installed and healthy on :${UDA_PORT}"
    exit 0
  fi
  sleep 2
done

systemctl status uda-xvfb.service uda-desktop.service uda-compat.service --no-pager || true
journalctl -u uda-compat.service -n 200 --no-pager || true
exit 1
