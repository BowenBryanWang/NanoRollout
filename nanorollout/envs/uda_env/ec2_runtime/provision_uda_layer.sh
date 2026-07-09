#!/usr/bin/env bash
# Install the UDA /v1 compatibility layer on an OSWorld-derived Ubuntu AMI.
set -euo pipefail

UDA_USER="${UDA_USER:-user}"
if ! id "${UDA_USER}" >/dev/null 2>&1; then
  if id user >/dev/null 2>&1; then
    UDA_USER="user"
  elif id ubuntu >/dev/null 2>&1; then
    UDA_USER="ubuntu"
  fi
fi
UDA_HOME="${UDA_HOME:-/home/${UDA_USER}}"
UDA_WORKSPACE="${UDA_WORKSPACE:-${UDA_HOME}}"
UDA_PORT="${UDA_PORT:-8080}"
DISPLAY_VALUE="${UDA_DISPLAY:-${DISPLAY:-}}"
UDA_UID="$(id -u "${UDA_USER}")"
XDG_RUNTIME_DIR_VALUE="${UDA_XDG_RUNTIME_DIR:-/run/user/${UDA_UID}}"
DBUS_SESSION_BUS_ADDRESS_VALUE="${UDA_DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR_VALUE}/bus}"
XAUTHORITY_VALUE="${UDA_XAUTHORITY:-}"

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv \
  psmisc \
  xdotool scrot gnome-screenshot imagemagick x11-utils xclip xsel \
  curl jq ripgrep git unzip zip p7zip-full rsync tree \
  poppler-utils qpdf ghostscript libreoffice \
  ffmpeg mediainfo libimage-exiftool-perl \
  fonts-noto fonts-noto-cjk fonts-noto-color-emoji fonts-liberation \
  build-essential ca-certificates

if [[ -z "${DISPLAY_VALUE}" ]]; then
  for candidate in :0 :1 :2; do
    if sudo -u "${UDA_USER}" DISPLAY="${candidate}" XAUTHORITY="${XAUTHORITY_VALUE:-${XDG_RUNTIME_DIR_VALUE}/gdm/Xauthority}" xdpyinfo >/dev/null 2>&1; then
      DISPLAY_VALUE="${candidate}"
      break
    fi
  done
fi
DISPLAY_VALUE="${DISPLAY_VALUE:-:0}"
if [[ -z "${XAUTHORITY_VALUE}" ]]; then
  for candidate in \
    "${XDG_RUNTIME_DIR_VALUE}/gdm/Xauthority" \
    "${UDA_HOME}/.Xauthority"; do
    if [[ -s "${candidate}" ]]; then
      XAUTHORITY_VALUE="${candidate}"
      break
    fi
  done
fi
XAUTHORITY_VALUE="${XAUTHORITY_VALUE:-${XDG_RUNTIME_DIR_VALUE}/gdm/Xauthority}"

install -d -m 0755 /opt/uda-ec2
install -m 0755 /tmp/uda_compat_server.py /opt/uda-ec2/uda_compat_server.py
install -d -o "${UDA_USER}" -g "${UDA_USER}" -m 0777 /tmp_workspace /tmp_workspace/results

cat >/etc/systemd/system/uda-compat.service <<EOF
[Unit]
Description=UDA EC2 /v1 compatibility API
After=network-online.target osworld.service
Wants=network-online.target

[Service]
Type=simple
Environment=DISPLAY=${DISPLAY_VALUE}
Environment=HOME=${UDA_HOME}
Environment=XAUTHORITY=${XAUTHORITY_VALUE}
Environment=XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR_VALUE}
Environment=DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS_VALUE}
Environment=UDA_HOME=${UDA_HOME}
Environment=UDA_WORKSPACE=${UDA_WORKSPACE}
Environment=UDA_COMPAT_HOST=0.0.0.0
Environment=UDA_COMPAT_PORT=${UDA_PORT}
WorkingDirectory=${UDA_WORKSPACE}
ExecStartPre=-/usr/bin/fuser -k ${UDA_PORT}/tcp
ExecStart=/usr/bin/python3 /opt/uda-ec2/uda_compat_server.py
Restart=always
RestartSec=3
User=${UDA_USER}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable uda-compat.service
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${UDA_PORT}/tcp" || true
fi
systemctl restart uda-compat.service

# OSWorld images conventionally expose noVNC on 5910 and pyautogui server
# on 5000. UDA rollouts need 8080; firewall is usually inactive on these
# AMIs, but open it if ufw exists and is enabled.
if command -v ufw >/dev/null 2>&1 && ufw status | grep -qi active; then
  ufw allow "${UDA_PORT}/tcp" || true
fi

sleep 2
curl -fsS "http://127.0.0.1:${UDA_PORT}/v1/sandbox" >/tmp/uda-compat-health.json
echo "UDA compat layer installed and healthy on :${UDA_PORT}"
