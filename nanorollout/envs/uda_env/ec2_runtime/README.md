# UDA EC2 Runtime

This directory contains the AWS EC2 runtime for UDA/NanoRollout tasks.
It runs VM-backed OSWorld desktop environments instead of Docker/Modal
containers while exposing the same UDA `/v1/*` API surface on port `8080`.

## Current Delivery

Region: `ap-southeast-1` (Singapore)

| Profile | Purpose | AMI | Instance | Status |
| --- | --- | --- | --- | --- |
| `general-root` | OSWorld Ubuntu 22.04 GNOME desktop + UDA `/v1` API | `ami-0e0244ba3257d200d` | `t3.xlarge` | validated |
| `multimedia` / `media` | General root + Blender/video/audio stack. Current AMI has Ubuntu apt Blender 3.0.1. | `ami-0982def14f5ab6546` | `t3.xlarge` | validated |
| `multimedia-blender5` | Next multimedia build target with Blender 5.1.2 as default `blender` and apt Blender as optional `blender3`. | fill after build | `t3.xlarge` | build-ready |
| `datascience` / `bi` | General root + BI/notebook/analytics stack | `ami-0a853376fa22ebf11` | `t3.xlarge` | validated |

Shared AWS resources:

- launch template: `lt-03862713037af59fe`, default version `9`
- subnet: `subnet-0c85c17f888605401`
- security group: `sg-029a65325aae8f739`
- instance profile: `uda-gym-ec2-instance-profile`
- official OSWorld source AMI: `ami-0d23263edb96951d8`
  (`osworld_client_image_30G_0719`, owner `366177350716`)

The full operator handoff is in `UDA_ENV_EC2_USAGE.md`. The machine-readable
profile registry is `env_profiles.yaml`.

## Runtime Contract

Every delivered AMI runs:

- `gdm.service`
- `osworld.service`
- `websockify.service`
- `uda-compat.service`

The desktop is the OSWorld GNOME/Xorg session on `DISPLAY=:0` with
`XAUTHORITY=/run/user/1000/gdm/Xauthority`. The compatibility server
listens on `http://<instance-ip>:8080` and supports:

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

See `API_CONTRACT.md` for source-of-truth alignment with
agent-infra/sandbox and Anthropic computer-use semantics.

## Run

Use the launch template for the default base environment:

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

Use a profile AMI when a task needs heavy software:

```bash
EC2_AMI_ID=ami-0982def14f5ab6546 EC2_ENV_PROFILE=multimedia ...
EC2_AMI_ID=ami-0a853376fa22ebf11 EC2_ENV_PROFILE=datascience ...
```

For ALE/Blender tasks with generated `.blend` files carrying Blender 5.0/5.1
headers, build and validate `multimedia-blender5` before use. The profile
provision script installs Blender 5.1.2 under `/opt/blender`, links it as
`/usr/local/bin/blender`, and preserves apt Blender as `/usr/local/bin/blender3`
when the Ubuntu package is present.

## Validate

Run base runtime smoke:

```bash
.venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/smoke_nanorollout_ec2_runtime.py
```

Run profile smoke:

```bash
.venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/smoke_profile_ami.py \
  --ami-id ami-0982def14f5ab6546 \
  --profile-name multimedia \
  --instance-type t3.xlarge

.venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/smoke_profile_ami.py \
  --ami-id <new-blender5-ami> \
  --profile-name multimedia-blender5 \
  --instance-type t3.xlarge

.venv/bin/python nanorollout/envs/uda_env/ec2_runtime/aws/smoke_profile_ami.py \
  --ami-id ami-0a853376fa22ebf11 \
  --profile-name datascience \
  --instance-type t3.xlarge
```

If the local machine has a global HTTP proxy or virtual NIC enabled, wrap
AWS commands with:

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  <command>
```

## Build

Build the OSWorld base AMI with `aws/build_uda_ec2_ami.py`.

Build profile AMIs with `aws/build_profile_ami.py`. The default profile
provision transport is SSM, which is the validated path for OSWorld-based
instances:

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

Legacy clean-Ubuntu artifacts remain in `env_profiles.yaml` only for
regression comparison. They are not the production runtime path.
