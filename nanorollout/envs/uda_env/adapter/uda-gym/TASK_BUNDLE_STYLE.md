# UDA-Gym Task Bundle Style

Each UDA-Gym task is a self-contained directory:

```text
<task_id>/
  meta.json
  instruction.md
  exec/
  hidden/
  setup.sh
  gt/
  check.sh
```

Optional files may be added when useful:

```text
<task_id>/
  task.yaml
  spec.yaml
  runtime.yaml
  surface.yaml
  check.yaml
```

## Required Files

### `meta.json`

Task metadata used by the runner.

Required fields:

```json
{
  "id": "<task_id>",
  "driver": "uda-gym",
  "timeout_seconds": 1800
}
```

Recommended fields:

```json
{
  "name": "<short human-readable name>",
  "category": "<task family>",
  "source": "<generation source>",
  "runtime": {
    "type": "ec2",
    "profile": "multimedia",
    "required_software": ["blender"]
  }
}
```

`runtime` is optional. Use it when a task requires a specific software stack.
It declares generic runtime needs only. Do not put AMI ids, launch templates,
subnet ids, security groups, or account-specific deployment details inside task
bundles; the runner resolves `profile` through `env_profiles.yaml`.

### `instruction.md`

The only task instruction shown to the agent.

It must include:

- the user-facing task request;
- visible input paths under `/tmp_workspace/`;
- exact output paths under `/tmp_workspace/results/`;
- required schemas, field names, value enums, sorting rules, rounding rules, and precision;
- which GUI/browser surfaces are available when the task requires them.

It must not include:

- hidden paths;
- setup or grading details;
- ground-truth file names;
- session IDs, tokens, or verifier endpoints;
- reward implementation details.

### `exec/`

Agent-visible files staged before the rollout.

Staging rule:

```text
exec/<name> -> /tmp_workspace/<name>
```

Use `exec/` for:

- visible context documents;
- source data;
- local repos;
- media/assets the agent may inspect;
- starter files or templates.

Do not put hidden state, expected answers, verifier inputs, admin tokens, or setup-only assets in `exec/`.

### `hidden/`

Setup-only files staged before `setup.sh` and removed before the agent starts.

Staging rule:

```text
hidden/ -> /tmp_workspace/.uda_hidden/
```

Use `hidden/` for:

- mock website seed state;
- private setup manifests;
- service initialization payloads;
- files needed only to create the initial environment.

`hidden/` contents must never be copied into `/tmp_workspace/context` or any other agent-visible path.

### `setup.sh`

Hidden pre-agent setup script.

Execution context:

```text
working directory: task bundle root
visible workspace: /tmp_workspace
hidden setup assets: /tmp_workspace/.uda_hidden
```

Required behavior:

- start with `#!/usr/bin/env bash` and `set -euo pipefail`;
- create `/tmp_workspace/results`;
- verify required `exec/` files are present after staging;
- initialize any required services, apps, browser sessions, or mock websites;
- open every required GUI/browser/desktop software surface before the agent starts;
- leave each required software surface in a ready-to-use state, such as a browser tab on the seeded mock website, a PDF open in the viewer, a spreadsheet open in the office app, an editor open to the project, or a media file loaded in the relevant application;
- keep secrets and hidden setup assets out of agent-visible paths;
- exit nonzero if setup fails.

For CUA-Gym Hub mock websites in hardened mode, `setup.sh` should:

- seed state using the harness-held admin token;
- open Chrome to the one-time `launch_url`;
- avoid exposing the real `sid` in visible files, command-line instructions, or URLs.

### `gt/`

Hidden ground-truth files injected only after the agent finishes.

Staging rule:

```text
gt/ -> /tmp_workspace/gt/
```

Use `gt/` for:

- expected outputs;
- answer keys;
- private schemas;
- verifier reference data;
- thresholds or tolerance tables that should not be visible during rollout.

`gt/` must not be available before scoring.

### `check.sh`

Hidden post-agent evaluator.

Execution context:

```text
working directory: task bundle root
agent outputs: /tmp_workspace/results
ground truth: /tmp_workspace/gt
```

Required behavior:

- start with `#!/usr/bin/env bash` and `set -euo pipefail`;
- read agent outputs from `/tmp_workspace/results`;
- read hidden references from `/tmp_workspace/gt`;
- inspect GUI/mock/service state when it is part of the task;
- compute deterministic scores;
- print one JSON object as the final stdout line;
- exit zero when grading completed, even if the task score is low;
- exit nonzero only when the evaluator itself failed.

Required final JSON shape:

```json
{
  "overall_score": 0.0,
  "subscores": {
    "field_name": 0.0
  },
  "errors": []
}
```

`overall_score` must be numeric in `[0, 1]`.

## Optional Files

### `task.yaml`

Structured task metadata. If present, it should mirror `instruction.md` and `meta.json`, not replace them.

### `spec.yaml`

Generation-time specification. It may describe primitives, assets, anchors, and intended difficulty. It is not agent-visible.

### `runtime.yaml`

Optional runtime/profile declaration. If present, it should mirror
`meta.json.runtime`.

Recommended shape:

```yaml
runtime:
  type: ec2
  profile: multimedia
  required_software:
    - blender
```

### `surface.yaml`

Manifest of required interfaces.

Recommended shape:

```yaml
surfaces:
  - type: browser
    name: wandb
    required: true
  - type: cli
    name: local_workspace
    required: true
```

### `check.yaml`

Reward design seed. It may describe expected deliverables and scoring criteria, but `check.sh` is the executable source of truth.

## Runtime Order

The runner executes a bundle in this order:

1. Create `/tmp_workspace` and `/tmp_workspace/results`.
2. Copy `exec/*` into `/tmp_workspace/`.
3. Copy `hidden/*` into `/tmp_workspace/.uda_hidden/`.
4. Run `setup.sh`.
5. Remove `/tmp_workspace/.uda_hidden/`.
6. Run the agent with `instruction.md`.
7. Copy `gt/*` into `/tmp_workspace/gt/`.
8. Run `check.sh`.
9. Parse the final JSON object printed by `check.sh`.

## Visibility Rules

Agent-visible before rollout:

- `instruction.md`;
- files staged from `exec/`;
- initialized and opened GUI/browser/desktop app surfaces;
- `/tmp_workspace/results/`.

Hidden until scoring:

- `hidden/`;
- `setup.sh`;
- `gt/`;
- `check.sh`;
- admin tokens;
- verifier endpoints;
- expected answers.

## Output Rules

Agent deliverables should be machine-verifiable.

Preferred outputs:

- `.json`;
- `.yaml`;
- `.csv` / `.tsv`;
- `.xlsx`;
- patches or source files;
- deterministic app or mock-state mutations.

Avoid scored free-form documents such as `summary.md`, `report.md`, or open-ended prose unless the task also includes a rigid machine-verifiable structure.
