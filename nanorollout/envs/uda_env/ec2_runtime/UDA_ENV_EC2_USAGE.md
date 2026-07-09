# UDA Env on AWS EC2 Delivery Guide

This is the clean handoff for the UDA EC2 runtime in AWS Singapore. It
covers the delivered base AMI, validated profile AMIs, how to run them
from NanoRollout, and the validation gates required before future changes.

## Delivered Resources

Region: `ap-southeast-1`

Shared infrastructure:

| Resource | Value |
| --- | --- |
| AWS account | `572885593698` |
| IAM user used for delivery | `arn:aws:iam::572885593698:user/ldj-test` |
| Launch template | `lt-03862713037af59fe` |
| Launch template default version | `9` |
| Subnet | `subnet-0c85c17f888605401` |
| Security group | `sg-029a65325aae8f739` |
| Instance profile | `uda-gym-ec2-instance-profile` |
| Default instance type | `t3.xlarge` |

Validated AMIs:

| Profile | Alias | AMI | AMI name | Disk | Snapshot |
| --- | --- | --- | --- | --- | --- |
| `general-root` | `general` | `ami-0e0244ba3257d200d` | `uda-osworld-verified-v1-base-1783061353` | 80 GB | see AMI block device |
| `multimedia` | `media` | `ami-0982def14f5ab6546` | `uda-multimedia-profile-20260703-codex` | 120 GB | `snap-06cd8150463dad8cf` |
| `datascience` | `bi` | `ami-0a853376fa22ebf11` | `uda-datascience-bi-profile-metabase-v04915-20260703c` | 120 GB | `snap-0c95a35f4ef3d8c86` |

Base source:

- official OSWorld source AMI: `ami-0d23263edb96951d8`
- source region: `us-east-1`
- source name: `osworld_client_image_30G_0719`
- source owner: `366177350716`
- Singapore copy: `ami-02d209e2d203facdb`

## Base Runtime

`general-root` is the default runtime. It keeps OSWorld's Ubuntu 22.04
GNOME desktop and adds the UDA `/v1/*` compatibility server.

Desktop/runtime services:

- `gdm.service`
- `osworld.service`
- `websockify.service`
- `uda-compat.service`

Desktop environment:

- `DISPLAY=:0`
- `XAUTHORITY=/run/user/1000/gdm/Xauthority`
- screen size validated at `1920x1080`
- noVNC/x11vnc provided by OSWorld services

Included general software:

- Google Chrome
- Visual Studio Code
- LibreOffice suite
- Evince PDF reader
- PDF tools: poppler, qpdf, ghostscript, mupdf-tools
- GIMP
- Python, Node.js/npm, git, ripgrep, build basics
- ffmpeg, mediainfo, exiftool
- xdotool, scrot, gnome-screenshot, pyautogui screenshot fallback
- Latin/CJK/emoji fonts

## Profiles

Use `general-root` unless the task needs heavy domain software. Use
profile AMIs through `EC2_AMI_ID` or a resolver that reads
`env_profiles.yaml`.

`multimedia` / `media` includes:

- Blender
- Kdenlive
- OpenShot
- Shotcut
- Audacity
- HandBrake CLI/GUI
- VLC
- OBS Studio
- frei0r/mlt plugins
- the general root office/browser/PDF/image stack

`datascience` / `bi` includes:

- Grafana on port `3000`
- Metabase on port `3001`
- JupyterLab on port `8888`
- PostgreSQL
- Apache Superset CLI
- Streamlit, Dash
- DuckDB, SQLite, SQLite Browser
- R
- pandas, numpy, scipy, scikit-learn, statsmodels
- plotly, bokeh, altair, seaborn, matplotlib
- dbt-core, dbt-duckdb
- Great Expectations

Metabase is pinned to `v0.49.15` for OpenJDK 17 compatibility on Ubuntu
22.04. Do not switch it back to `latest` without a service health smoke;
newer jars may require Java 21.

## API Contract

The EC2 runtime is designed to look like the Docker-backed UDA sandbox to
NanoRollout. A healthy instance must answer:

```bash
curl http://<public-ip>:8080/v1/sandbox
```

Supported endpoints:

- `GET /v1/sandbox`
- `GET /v1/code/info`
- `POST /v1/shell/create`
- `POST /v1/shell/sessions/create`
- `POST /v1/shell/exec`
- `POST /v1/file/read`
- `POST /v1/file/write`
- `POST /v1/file/str_replace_editor`
- `POST /v1/code/execute`
- `POST /v1/jupyter/sessions/create`
- `POST /v1/computer-use/action`

The shell/file/code/Jupyter surface follows the `agent-infra/sandbox`
shape used by NanoRollout. The computer-use surface follows the Anthropic
computer-use action/result shape.

## Run

Run on the default base AMI through the launch template:

```bash
ENV_TYPE=ec2 \
EC2_REGION=ap-southeast-1 \
EC2_LAUNCH_TEMPLATE_ID=lt-03862713037af59fe \
EC2_INSTANCE_TYPE=t3.xlarge \
EC2_SUBNET_ID=subnet-0c85c17f888605401 \
EC2_SECURITY_GROUP_IDS=sg-029a65325aae8f739 \
EC2_IAM_INSTANCE_PROFILE=uda-gym-ec2-instance-profile \
EC2_ENV_PROFILE=general-root \
EC2_WORKSPACE_DIR=/home/user \
examples/eval/uda/run_uda_bench.sh
```

Run with the multimedia profile:

```bash
ENV_TYPE=ec2 \
EC2_REGION=ap-southeast-1 \
EC2_AMI_ID=ami-0982def14f5ab6546 \
EC2_INSTANCE_TYPE=t3.xlarge \
EC2_SUBNET_ID=subnet-0c85c17f888605401 \
EC2_SECURITY_GROUP_IDS=sg-029a65325aae8f739 \
EC2_IAM_INSTANCE_PROFILE=uda-gym-ec2-instance-profile \
EC2_ENV_PROFILE=multimedia \
EC2_WORKSPACE_DIR=/home/user \
examples/eval/uda/run_uda_bench.sh
```

Run with the data science / BI profile:

```bash
ENV_TYPE=ec2 \
EC2_REGION=ap-southeast-1 \
EC2_AMI_ID=ami-0a853376fa22ebf11 \
EC2_INSTANCE_TYPE=t3.xlarge \
EC2_SUBNET_ID=subnet-0c85c17f888605401 \
EC2_SECURITY_GROUP_IDS=sg-029a65325aae8f739 \
EC2_IAM_INSTANCE_PROFILE=uda-gym-ec2-instance-profile \
EC2_ENV_PROFILE=datascience \
EC2_WORKSPACE_DIR=/home/user \
examples/eval/uda/run_uda_bench.sh
```

Run Codex CLI with local Codex auth:

```bash
CODEX_AUTH_JSON="${HOME}/.codex/auth.json" \
OUTPUT_DIR="./results/uda-wildclaw-v1-codex-ec2-smoke" \
BENCH=wildclaw-v1 \
INSTANCE_ID=06_Safety_Alignment_task_1_file_overwrite \
MODEL_NAME=default \
ENV_TYPE=ec2 \
EC2_REGION=ap-southeast-1 \
EC2_LAUNCH_TEMPLATE_ID=lt-03862713037af59fe \
EC2_INSTANCE_TYPE=t3.xlarge \
EC2_SUBNET_ID=subnet-0c85c17f888605401 \
EC2_SECURITY_GROUP_IDS=sg-029a65325aae8f739 \
EC2_IAM_INSTANCE_PROFILE=uda-gym-ec2-instance-profile \
EC2_ENV_PROFILE=general-root \
bash examples/eval/uda/run_codex_oauth.sh
```

Run Claude Code when auth is available:

```bash
CLAUDE_CODE_OAUTH_TOKEN=... \
MODEL_NAME=claude-sonnet-4-6 \
OUTPUT_DIR="./results/uda-wildclaw-v1-claude-code-ec2" \
BENCH=wildclaw-v1 \
INSTANCE_ID=06_Safety_Alignment_task_1_file_overwrite \
ENV_TYPE=ec2 \
EC2_REGION=ap-southeast-1 \
EC2_LAUNCH_TEMPLATE_ID=lt-03862713037af59fe \
EC2_INSTANCE_TYPE=t3.xlarge \
EC2_SUBNET_ID=subnet-0c85c17f888605401 \
EC2_SECURITY_GROUP_IDS=sg-029a65325aae8f739 \
EC2_IAM_INSTANCE_PROFILE=uda-gym-ec2-instance-profile \
EC2_ENV_PROFILE=general-root \
bash examples/eval/uda/run_claude_code_oauth.sh
```

`ANTHROPIC_API_KEY` can be used instead of `CLAUDE_CODE_OAUTH_TOKEN`.
For Bedrock-backed Claude Code, export `AWS_BEARER_TOKEN_BEDROCK`.

## Validate

Use the NanoRollout virtualenv for integration tests. If the host has a
global proxy or virtual NIC, clear proxy variables:

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  <command>
```

Base preflight and runtime smoke:

```bash
.venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/preflight_uda_ec2.py
.venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/smoke_nanorollout_ec2_runtime.py
```

Raw `/v1` HTTP smoke against an already-running instance:

```bash
python nanorollout/envs/uda_env/ec2_runtime/aws/smoke_uda_ec2_runtime.py http://<public-ip>:8080
```

GUI smoke against an already-running instance:

```bash
python nanorollout/envs/uda_env/ec2_runtime/aws/smoke_gui_ec2_runtime.py \
  http://<public-ip>:8080 \
  --screenshot-dir /tmp/uda-ec2-gui-smoke
```

Profile smoke:

```bash
.venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/smoke_profile_ami.py \
  --ami-id ami-0982def14f5ab6546 \
  --profile-name multimedia \
  --instance-type t3.xlarge

.venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/smoke_profile_ami.py \
  --ami-id ami-0a853376fa22ebf11 \
  --profile-name datascience \
  --instance-type t3.xlarge
```

The data science profile smoke waits for:

- `grafana-server.service` active and `http://127.0.0.1:3000/api/health`
- `metabase.service` active and `http://127.0.0.1:3001/api/health`
- `uda-jupyter.service` active and `http://127.0.0.1:8888/api`
- `postgresql.service` active

Validate installed-agent rollout artifacts:

```bash
.venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/validate_installed_agent_rollout.py \
  --expected-agent codex \
  ./results/uda-wildclaw-v1-codex-ec2-smoke
```

## Build And Rebuild

Build the base AMI:

```bash
/tmp/uda-aws-boto3-venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/build_uda_ec2_ami.py \
  --region ap-southeast-1 \
  --source-region us-east-1 \
  --source-ami ami-0d23263edb96951d8 \
  --subnet-id subnet-0c85c17f888605401 \
  --security-group-id sg-029a65325aae8f739 \
  --instance-profile uda-gym-ec2-instance-profile \
  --instance-type t3.xlarge \
  --volume-size 80 \
  --launch-template-id lt-03862713037af59fe \
  --provision-method osworld-http
```

Build the multimedia profile:

```bash
/tmp/uda-aws-boto3-venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/build_profile_ami.py \
  --region ap-southeast-1 \
  --source-ami ami-0e0244ba3257d200d \
  --profile-name multimedia \
  --provision-script nanorollout/envs/uda_env/ec2_runtime/provision_multimedia_profile_ami.sh \
  --subnet-id subnet-0c85c17f888605401 \
  --security-group-id sg-029a65325aae8f739 \
  --instance-profile uda-gym-ec2-instance-profile \
  --instance-type t3.xlarge \
  --volume-size 120
```

Build the data science / BI profile:

```bash
/tmp/uda-aws-boto3-venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/build_profile_ami.py \
  --region ap-southeast-1 \
  --source-ami ami-0e0244ba3257d200d \
  --profile-name datascience \
  --provision-script nanorollout/envs/uda_env/ec2_runtime/provision_datascience_profile_ami.sh \
  --subnet-id subnet-0c85c17f888605401 \
  --security-group-id sg-029a65325aae8f739 \
  --instance-profile uda-gym-ec2-instance-profile \
  --instance-type t3.xlarge \
  --volume-size 120
```

`build_profile_ami.py` defaults to SSM provision transport because it is
the validated path for OSWorld-derived AMIs. The legacy user-data path is
still available through `--provision-transport user-data`.

## Operational Notes

Security group ingress should expose only port `8080` to trusted runner
IPs by default. VNC `5910`, SSH `22`, and BI ports `3000/3001/8888/8088`
should remain closed unless explicitly needed.

Instances launched by NanoRollout are tagged with `Project=UDA-Gym` and
should terminate through `EC2SandboxRuntime.cleanup()`. Check for
leftovers with:

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /tmp/uda-aws-boto3-venv/bin/python - <<'PY'
import boto3, json
ec2 = boto3.client("ec2", region_name="ap-southeast-1")
resp = ec2.describe_instances(Filters=[
    {"Name": "tag:Project", "Values": ["UDA-Gym"]},
    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
])
items = []
for reservation in resp.get("Reservations", []):
    for inst in reservation.get("Instances", []):
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        items.append({
            "InstanceId": inst["InstanceId"],
            "State": inst["State"]["Name"],
            "Name": tags.get("Name"),
            "Profile": tags.get("Profile"),
            "Purpose": tags.get("Purpose"),
            "PublicIpAddress": inst.get("PublicIpAddress"),
        })
print(json.dumps(items, indent=2))
PY
```

Current delivery residue check result: no pending/running/stopping/stopped
`Project=UDA-Gym` instances remained after validation.

## Change Policy

- Keep `general-root` broad, GUI-capable, and OSWorld-compatible.
- Put heavy specialized software into profile AMIs.
- Never promote a profile AMI without `smoke_profile_ami.py`.
- Never promote a data science AMI unless Grafana, Metabase, Jupyter, and
  PostgreSQL health checks pass.
- Keep failed or experimental AMIs out of `env_profiles.yaml` production
  aliases.
