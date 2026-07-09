# UDA EC2 AWS Scripts

Scripts in this directory are grouped by lifecycle stage.

## Build

- `build_uda_ec2_ami.py`: builds the OSWorld-derived `general-root` AMI
  and can update the launch template.
- `build_profile_ami.py`: builds profile AMIs from a validated base AMI.
  The default provision transport is SSM. Use
  `--provision-transport user-data` only for legacy experiments.
- `build_ubuntu_root_ami.py`: legacy clean-Ubuntu/Xvfb root builder.
  Keep for regression comparison; do not use for the production OSWorld
  runtime path.

## Smoke

- `preflight_uda_ec2.py`: validates AWS/NanoRollout prerequisites without
  launching a real instance, except for EC2 DryRun.
- `smoke_nanorollout_ec2_runtime.py`: launches an EC2 runtime through
  NanoRollout and verifies shell/file/screenshot/cleanup behavior.
- `smoke_uda_ec2_runtime.py`: raw HTTP `/v1` endpoint smoke for an
  already-running instance.
- `smoke_gui_ec2_runtime.py`: raw HTTP GUI smoke for an already-running
  instance; captures desktop/app screenshots.
- `smoke_profile_ami.py`: launches a profile AMI and validates marker,
  command presence, screenshot, cleanup, and data science service health.

## Rollout Validation

- `validate_installed_agent_rollout.py`: validates recovered artifacts for
  Codex or Claude Code rollouts and confirms EC2 cleanup.
- `validate_claude_rollout.py`: legacy Claude-only validator; prefer
  `validate_installed_agent_rollout.py`.

## Proxy Bypass

When the host has a global proxy or virtual network adapter enabled, run
AWS/direct-EC2 checks with proxy variables cleared:

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  <script command>
```
