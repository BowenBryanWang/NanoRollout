# NanoRollout UDA-Gym Code Handoff

Last updated: 2026-07-27

Branch: `bowen/uda-gym` on `BowenBryanWang/NanoRollout`.

The full AWS resource and AMI runbook remains
`UDA_ENV_EC2_USAGE.md`. This note describes the code paths that differ from
upstream NanoRollout and must be preserved when rebasing.

The dependency lock pins `borb==2.1.25`. The 3.0.5-3.0.7 wheels contain
duplicate conflicting archive entries that strict `uv sync` refuses to
extract, while the evaluator imports use the borb 2.x API.

## Native Driver

`nanorollout/envs/uda_env/driver/uda_gym.py` implements the native bundle
lifecycle:

1. load `task.yaml` and/or `instruction.md`;
2. replace stale placeholder task instructions with real `instruction.md`;
3. stage `exec/` into `/tmp_workspace`;
4. create a randomized per-rollout harness state directory;
5. pass harness variables only to hidden `setup.sh` and `check.sh`;
6. stage and remove `hidden/` before the agent;
7. inject `gt/` only after the agent;
8. parse the final JSON object from checker stdout.

Both `returncode` and `exit_code` runtime result shapes are accepted. Do not
regress to checking only one shape; EC2 and SDK adapters have emitted both.

## Harness Secret Boundary

`harness_env.tsv` lists host variable names such as
`CUA_GYM_ADMIN_TOKEN`. Values are read from the rollout process environment and
passed only to setup/check execution.

`UDA_GYM_HARNESS_STATE_DIR` is generated per rollout and never added to
`/tmp_workspace`, profile scripts, task instructions, or the agent environment.
Mock setup/check scripts use it to exchange randomized sid and verifier
metadata.

This prevents accidental exposure; it is not a defense against a genuinely
root-privileged agent that is allowed to enumerate all host process/runtime
state. Keep agent privilege separation enabled where the benchmark threat model
requires stronger isolation.

## Runtime Profile Routing

`nanorollout/envs/uda_env/runtime_profile.py`:

- resolves profile aliases;
- inherits parent profile capabilities;
- normalizes common software names;
- rejects unvalidated profiles;
- clears conflicting launch-template fields for explicit profile AMIs;
- keeps explicit CLI/runtime choices authoritative where safe.

The task bundle should normally carry only:

```yaml
runtime:
  type: ec2
  profile: general-root
```

Do not embed AMI, subnet, security group, launch template, or account
credentials in task bundles.

## Screenshot Evidence

Both installed-agent and UDAAgent runners capture:

```text
screenshots/pre_rollout.png
screenshots/pre_rollout.json
```

Capture occurs after setup and before the agent acts. It retries blank/empty
screenshots and records quality diagnostics. CUA-Gym final gating requires this
artifact.

## Run

```bash
export PATH="$PWD/.venv/bin:$PATH"
export AWS_REGION=ap-southeast-1

BENCH=uda-gym \
UDA_TASKS_DIR=/absolute/path/to/tasks \
INSTANCE_ID=<task_id> \
AGENT=codex \
ENV_TYPE=ec2 \
EC2_ENV_PROFILE=general-root \
EC2_TERMINATE_ON_CLEANUP=true \
bash examples/eval/uda/run_codex_oauth.sh
```

AWS credential selection follows boto3/CLI precedence. Set `AWS_PROFILE` when
the office machine has multiple accounts. Confirm identity before scale:

```bash
aws sts get-caller-identity
```

Expected account is `572885593698`, region `ap-southeast-1`.

## Tests

```bash
.venv/bin/python -m pytest -q tests/uda
python -m compileall -q \
  nanorollout/envs/uda_env \
  nanorollout/harness/runner/uda
```

Before a real scale run, execute one task and verify:

- setup succeeded;
- the screenshot shows the intended app/document/browser;
- trajectory exists;
- reward JSON matches trajectory behavior;
- `EC2_TERMINATE_ON_CLEANUP=true`;
- no `Project=UDA-Gym` instance remains running.

## Rebase Hotspots

When rebasing onto upstream NanoRollout, inspect these files carefully:

```text
nanorollout/envs/uda_env/adapter/PROTOCOL.md
nanorollout/envs/uda_env/driver/uda_gym.py
nanorollout/envs/uda_env/runtime_profile.py
nanorollout/harness/runner/uda/installed.py
nanorollout/harness/runner/uda/uda_agent.py
```

Do not resolve conflicts by dropping:

- randomized harness state;
- hidden harness environment handling;
- placeholder-instruction replacement;
- runtime profile inheritance/normalization;
- pre-rollout screenshot evidence;
- `returncode`/`exit_code` compatibility.
