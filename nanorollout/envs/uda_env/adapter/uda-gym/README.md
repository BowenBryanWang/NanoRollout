# Native UDA-Gym Task Bundle Contract

This adapter is the native UDA-Gym task format. Use it for generated UDA tasks
instead of forcing them into the WildClaw layout.

## Status From The First Demo

Demo task: `adapter/uda-gym/codex100_002`

Rollout result:

- EC2 setup completed.
- `exec/` was staged before the agent.
- hidden `setup.sh` ran before the agent.
- `setup.sh` injected W&B state into CUA-Gym Hub and opened Google Chrome at the
  seeded `?sid=...` URL.
- hidden `check.sh` ran after the agent.
- reward was `1.0`.

Important caveat: the Codex trajectory solved the demo partly by reading Chrome
profile/local-storage artifacts and `/tmp/task_web_sid`, not by visually using
the dashboard. The bundle contract is correct, but this demo is only a
lifecycle smoke test. It is not benchmark-quality as a GUI-necessity task.
Future tasks must harden against this CLI bypass before they are used for
training or evaluation.

## NanoRollout Usage

The adapter is selected with `--bench uda-gym`; task ids are subdirectories
under this folder.

Controller-loop path:

```bash
BENCH=uda-gym INSTANCE_ID=codex100_002 \
  bash examples/eval/uda/run_uda_bench.sh
```

Installed Codex path on EC2:

```bash
BENCH=uda-gym INSTANCE_ID=codex100_002 \
  ENV_TYPE=ec2 EC2_ENV_PROFILE=general-root \
  bash examples/eval/uda/run_codex_oauth.sh
```

Direct `nro` equivalent:

```bash
nro run \
  --task uda \
  --agent codex \
  --bench uda-gym \
  --instance-id codex100_002 \
  --env-type ec2 \
  --ec2-env-profile general-root
```

## Directory Layout

Each task lives under:

```text
adapter/uda-gym/<task_id>/
  meta.json
  task.yaml
  instruction.md
  Dockerfile
  docker-compose.yaml
  exec/
  hidden/
  setup.sh
  gt/
  check.sh
  spec.yaml
  surface.yaml
  check.yaml
```

Required runtime files:

- `meta.json`: must include `"driver": "uda-gym"`.
- `task.yaml` or `instruction.md`: agent-facing instruction.
- `exec/`: files copied into `/tmp_workspace/` before the agent starts.
- `setup.sh`: hidden pre-agent setup script.
- `gt/`: ground truth copied into `/tmp_workspace/gt/` only after the agent
  finishes.
- `check.sh`: hidden evaluator run after `gt/` injection.

Recommended metadata files:

- `spec.yaml`: original UDA primitive/query metadata.
- `surface.yaml`: interface realization manifest.
- `check.yaml`: natural-language reward seeds.

## Lifecycle

The native driver executes the task in this order:

1. Create `/tmp_workspace` and `/tmp_workspace/results`.
2. Copy every top-level entry from `exec/` into `/tmp_workspace/`.
3. Copy `hidden/` to `/tmp_workspace/.uda_hidden`.
4. Run task-local `setup.sh`.
5. Delete `/tmp_workspace/.uda_hidden` and the temporary setup script.
6. Run the agent with only the instruction and visible environment.
7. Copy `gt/` into `/tmp_workspace/gt`.
8. Run task-local `check.sh`.
9. Parse the last JSON object printed by `check.sh` as the reward.

`setup.sh`, `hidden/`, `gt/`, and `check.sh` are harness artifacts. They must
not be copied into `exec/` and must not be mentioned in the instruction.

## CUA Mock Website Rules

For every CUA-Gym Hub mock used by a task:

1. Store schema-valid initial state under `hidden/cua_mock/`.
2. In `setup.sh`, generate a fresh `sid`.
3. Write `/tmp/task_web_sid`; for multi-mock tasks also write per-mock files
   like `/tmp/task_web_sid_wandb` if useful for `check.sh`.
4. POST the hidden state to:
   `https://cua-gym-<name>.xlang.ai/post?sid=<sid>`.
5. Verify:
   `https://cua-gym-<name>.xlang.ai/go?sid=<sid>`.
6. Open Google Chrome to every seeded mock URL:
   `https://cua-gym-<name>.xlang.ai/?sid=<sid>`.

Use a browser-like User-Agent for Hub API calls:

```python
headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
```

Do not put any of these in `exec/` or visible `context/`:

- CUA initial-state JSON
- `cua_mock_session.json`
- `warmup.sh`
- `/post?sid`
- `/go?sid`
- `initial_state`
- `current_state`
- `state_diff`

The instruction should say things like:

- "The seeded W&B dashboard is already open in Chrome."
- "Use the Jira board already open in Chrome."
- "Compare the open CRM workspace with the local export."

It should not expose the `sid` or tell the agent to call the state API.

## `setup.sh` Requirements

`setup.sh` should:

- fail fast with `set -euo pipefail`;
- create `/tmp_workspace/results`;
- assert required visible files exist;
- read hidden setup assets only from `/tmp_workspace/.uda_hidden`;
- seed every required mock/service;
- open every GUI/browser surface needed by the task;
- avoid copying hidden state into `/tmp_workspace/context`;
- print concise readiness lines only.

For GUI surfaces, opening the app is not optional. If a task involves three
mock websites, setup must open all three seeded URLs in Chrome before the agent
starts.

## `check.sh` Requirements

`check.sh` should:

- run after `gt/` injection;
- read `/tmp/task_web_sid` or the appropriate per-mock sid file;
- read CUA current state via `/go?sid=<sid>`;
- compare actual outputs against deterministic criteria;
- return partial credit as named numeric fields;
- include `overall_score`;
- print exactly one JSON object as the last meaningful stdout line.

The evaluator should check both local deliverables and GUI/mock state when the
task is hybrid.

## Agent-Facing Instruction Rules

The instruction must be natural and practitioner-like. It should include:

- what the user wants;
- where visible inputs are;
- which GUI/browser surface is already open or should be used;
- exact structured deliverables under `/tmp_workspace/results/`;
- allowed enums, required fields, counts, precision, and sorting rules.

The instruction must not include:

- primitive names;
- setup/check details;
- hidden file paths;
- mock state schemas;
- session IDs;
- CUA Hub readback/write endpoints;
- solution algorithms.

Primary deliverables must be machine-verifiable: JSON, CSV/TSV/XLSX, YAML,
patches, configs, deterministic app state mutations, or rigid tables. Avoid
free-form `summary.md` / `report.md` as scored outputs.

## Correctness Checklist

Before rollout:

- `meta.json` has `"driver": "uda-gym"`.
- `task.yaml` or `instruction.md` loads.
- `exec/` contains only agent-visible inputs.
- `hidden/` contains setup-only state/assets.
- `setup.sh` has `bash -n` clean syntax.
- `check.sh` has `bash -n` clean syntax.
- CUA mock names match CUA-Gym Hub schemas.
- `surface.yaml` names every actual interface.
- `check.yaml` covers every promised deliverable.

After rollout:

- trial log shows `uda-gym: staged ... -> /tmp_workspace/...`;
- trial log enters agent only after setup succeeds;
- trial log shows `Running uda-gym driver score()`;
- reward JSON contains `overall_score`;
- EC2 cleanup terminates the instance;
- trajectory is inspected for CLI bypasses.

## Anti-Bypass Notes

The first demo proved that a CLI-capable agent may inspect:

- `/tmp/task_web_sid`;
- Chrome command lines;
- Chrome Local Storage / Session Storage;
- fetched SPA bundles;
- default `/state` endpoints.

Treat these as disallowed solve paths. A rollout that gets high reward by using
any of them should be marked task-invalid even if `check.sh` returns
`overall_score: 1.0`.

For research-grade UDA tasks, do not rely on a single hidden status value that
is easily recoverable from browser storage. Prefer tasks where success requires
real interaction with the GUI state, such as:

- changing app state through buttons/forms;
- selecting records after visual filtering/sorting;
- reconciling information displayed across multiple open mock pages;
- using files downloaded through the browser;
- verifying visual labels/status badges that are not mirrored in local files;
- state mutations that `check.sh` reads through `/go?sid`.

If a task is meant to demonstrate GUI necessity, inspect the rollout trajectory.
High reward with no computer/browser actions is a signal to revise the task,
not a success.

## Release Gate For GUI-Necessity Tasks

Before accepting a generated task as training/eval data, run a CLI-only rollout
and inspect the trajectory. Reject or revise the task if the successful
trajectory reads any of:

- `/tmp/task_web_sid*`;
- `/tmp_workspace/.uda_hidden`;
- Chrome `Local Storage`, `Session Storage`, `History`, `Cookies`, or LevelDB
  files;
- CUA Hub `/go?sid=...`, `/state?sid=...`, `/post?sid=...`;
- mock state files or session manifests;
- browser process command lines solely to recover hidden URLs or sids.

For accepted GUI-necessity tasks, the decisive evidence should come from
computer/browser interaction or app state mutation, not from filesystem
forensics over the browser profile.
