#!/usr/bin/env bash
# Add the data science / BI profile layer on top of the UDA general-root AMI.
set -euo pipefail
exec > >(tee -a /var/log/uda-datascience-profile-provision.log /dev/console) 2>&1
set -x

PROFILE_NAME="${UDA_PROFILE_NAME:-datascience}"
MARKER_DIR="/opt/uda-ec2"
MARKER_JSON="${MARKER_DIR}/profile-${PROFILE_NAME}.json"
MARKER_READY="${MARKER_DIR}/profile-${PROFILE_NAME}.ready"
DS_HOME="/opt/uda-datascience"
DS_VENV="${DS_HOME}/venv"
METABASE_HOME="/opt/metabase"
METABASE_DATA="/var/lib/metabase"
METABASE_VERSION="${METABASE_VERSION:-v0.49.15}"
export METABASE_VERSION

export DEBIAN_FRONTEND=noninteractive

. /etc/os-release
UBUNTU_CODENAME="${VERSION_CODENAME:-jammy}"
cat >/etc/apt/sources.list.d/uda-ubuntu-updates.list <<EOF
deb http://hk.archive.ubuntu.com/ubuntu ${UBUNTU_CODENAME}-updates main restricted universe multiverse
EOF

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl wget gnupg software-properties-common apt-transport-https \
  openjdk-17-jre-headless \
  postgresql postgresql-client sqlite3 sqlitebrowser \
  r-base r-base-dev \
  graphviz graphviz-dev \
  libpq-dev libssl-dev libffi-dev libsasl2-dev libldap2-dev \
  python3-dev python3-venv python3-pip python3-tk \
  unixodbc unixodbc-dev \
  jq

install -d -m 0755 /etc/apt/keyrings
if [[ ! -s /etc/apt/keyrings/grafana.gpg ]]; then
  wget -q -O - https://apt.grafana.com/gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
  chmod 0644 /etc/apt/keyrings/grafana.gpg
fi
cat >/etc/apt/sources.list.d/grafana.list <<'EOF'
deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main
EOF
apt-get update
apt-get install -y --no-install-recommends grafana

install -d -m 0755 "${DS_HOME}" "${MARKER_DIR}"
python3 -m venv "${DS_VENV}"
"${DS_VENV}/bin/python" -m pip install --upgrade pip setuptools wheel
"${DS_VENV}/bin/pip" install --no-cache-dir \
  jupyterlab notebook ipykernel \
  pandas numpy scipy scikit-learn statsmodels \
  matplotlib seaborn plotly bokeh altair \
  duckdb polars pyarrow openpyxl xlrd xlsxwriter \
  sqlalchemy psycopg2-binary pymysql \
  streamlit dash apache-superset \
  dbt-core dbt-duckdb \
  great-expectations \
  rich \
  requests beautifulsoup4 lxml \
  boto3

cat >/usr/local/bin/uda-datascience-python <<EOF
#!/usr/bin/env bash
exec ${DS_VENV}/bin/python "\$@"
EOF
cat >/usr/local/bin/uda-jupyter-lab <<EOF
#!/usr/bin/env bash
exec ${DS_VENV}/bin/jupyter lab --ip=0.0.0.0 --no-browser --NotebookApp.token='' --NotebookApp.password='' "\$@"
EOF
cat >/usr/local/bin/uda-streamlit <<EOF
#!/usr/bin/env bash
exec ${DS_VENV}/bin/streamlit "\$@"
EOF
cat >/usr/local/bin/uda-superset <<EOF
#!/usr/bin/env bash
export SUPERSET_CONFIG_PATH=/etc/uda-superset/superset_config.py
exec ${DS_VENV}/bin/superset "\$@"
EOF
chmod 0755 /usr/local/bin/uda-datascience-python /usr/local/bin/uda-jupyter-lab /usr/local/bin/uda-streamlit /usr/local/bin/uda-superset

install -d -m 0755 /etc/uda-superset
if [[ ! -s /etc/uda-superset/secret_key ]]; then
  openssl rand -base64 48 >/etc/uda-superset/secret_key
  chmod 0600 /etc/uda-superset/secret_key
fi
cat >/etc/uda-superset/superset_config.py <<'EOF'
import os
from pathlib import Path

SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = Path("/etc/uda-superset/secret_key").read_text(encoding="utf-8").strip()
SQLALCHEMY_DATABASE_URI = os.environ.get(
    "SUPERSET_METADATA_DB",
    "sqlite:////var/lib/uda-superset/superset.db",
)
WTF_CSRF_ENABLED = False
TALISMAN_ENABLED = False
EOF
install -d -o user -g user -m 0755 /var/lib/uda-superset

if ! id metabase >/dev/null 2>&1; then
  useradd --system --home-dir "${METABASE_DATA}" --shell /usr/sbin/nologin metabase
fi
install -d -o metabase -g metabase -m 0755 "${METABASE_HOME}" "${METABASE_DATA}"
wget -q -O "${METABASE_HOME}/metabase.jar" "https://downloads.metabase.com/${METABASE_VERSION}/metabase.jar"
chown metabase:metabase "${METABASE_HOME}/metabase.jar"
cat >/usr/local/bin/metabase <<'EOF'
#!/usr/bin/env bash
exec java --add-opens java.base/java.nio=ALL-UNNAMED -jar /opt/metabase/metabase.jar "$@"
EOF
chmod 0755 /usr/local/bin/metabase

cat >/etc/systemd/system/metabase.service <<EOF
[Unit]
Description=Metabase BI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=metabase
Group=metabase
WorkingDirectory=${METABASE_DATA}
Environment=MB_JETTY_HOST=0.0.0.0
Environment=MB_JETTY_PORT=3001
Environment=MB_DB_FILE=${METABASE_DATA}/metabase.db
ExecStart=/usr/bin/java --add-opens java.base/java.nio=ALL-UNNAMED -jar ${METABASE_HOME}/metabase.jar
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/uda-jupyter.service <<EOF
[Unit]
Description=UDA JupyterLab
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=user
Group=user
WorkingDirectory=/home/user
Environment=HOME=/home/user
Environment=PATH=${DS_VENV}/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=${DS_VENV}/bin/jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --NotebookApp.token='' --NotebookApp.password=''
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable grafana-server.service metabase.service uda-jupyter.service postgresql.service

python3 - <<'PY'
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

commands = {
    "python": "/opt/uda-datascience/venv/bin/python --version",
    "jupyter": "/opt/uda-datascience/venv/bin/jupyter --version",
    "pandas": "/opt/uda-datascience/venv/bin/python -c 'import pandas; print(pandas.__version__)'",
    "duckdb": "/opt/uda-datascience/venv/bin/python -c 'import duckdb; print(duckdb.__version__)'",
    "streamlit": "/opt/uda-datascience/venv/bin/streamlit version",
    "superset": "/usr/local/bin/uda-superset version",
    "grafana-server": "grafana-server -v",
    "metabase": "test -s /opt/metabase/metabase.jar && echo metabase.jar",
    "psql": "psql --version",
    "sqlite3": "sqlite3 --version",
    "R": "R --version",
}
services = ["grafana-server", "metabase", "uda-jupyter", "postgresql"]
manifest = {
    "profile": "datascience",
    "metabase_version": os.environ.get("METABASE_VERSION", "v0.49.15"),
    "ports": {
        "grafana": 3000,
        "metabase": 3001,
        "jupyter": 8888,
        "superset_default": 8088,
        "streamlit_default": 8501,
    },
    "includes": [
        "Grafana",
        "Metabase",
        "JupyterLab",
        "Apache Superset CLI",
        "Streamlit",
        "Dash",
        "DuckDB",
        "PostgreSQL",
        "SQLite/SQLite Browser",
        "R",
        "pandas/numpy/scipy/scikit-learn/statsmodels",
        "plotly/bokeh/altair/seaborn/matplotlib",
        "dbt-core/dbt-duckdb",
        "Great Expectations",
    ],
    "commands": {},
    "services": {},
}
for name, command in commands.items():
    binary = command.split()[0]
    path = shutil.which(binary) if "/" not in binary else binary
    result = {"path": path}
    try:
        proc = subprocess.run(
            command,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
            timeout=30,
        )
        result["returncode"] = proc.returncode
        result["version_output"] = proc.stdout.splitlines()[:8]
    except Exception as exc:
        result["error"] = str(exc)
    manifest["commands"][name] = result

for svc in services:
    proc = subprocess.run(
        f"systemctl is-enabled {svc}.service",
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
    )
    manifest["services"][svc] = {
        "enabled": proc.stdout.strip(),
        "returncode": proc.returncode,
    }

Path("/opt/uda-ec2/profile-datascience.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
Path("/opt/uda-ec2/profile-datascience.ready").write_text("ok\n", encoding="utf-8")
PY

systemctl restart grafana-server.service || true
systemctl restart metabase.service || true
systemctl restart uda-jupyter.service || true
systemctl restart uda-compat.service || true
echo "UDA datascience profile installed"
