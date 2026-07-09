# UDA EC2 API Contract

The EC2 runtime intentionally exposes the same HTTP surface that
NanoRollout already uses for Docker-backed UDA environments. The goal is
runtime interchangeability: once a VM reaches `GET /v1/sandbox`, callers
should not need to know whether the underlying desktop is a container or
an EC2 instance.

## Contract Sources

### Sandbox, Shell, File, Code, and Jupyter

These endpoints are aligned to the `agent-infra/sandbox` API surface and
the generated `agent_sandbox` Python SDK:

- `GET /v1/sandbox`
- `POST /v1/shell/sessions/create`
- `POST /v1/shell/exec`
- `POST /v1/file/read`
- `POST /v1/file/write`
- `POST /v1/file/str_replace_editor`
- `POST /v1/code/execute`
- `POST /v1/jupyter/sessions/create`

Local verification points:

- `.venv/lib/python3.12/site-packages/agent_sandbox/sandbox/raw_client.py`
- `.venv/lib/python3.12/site-packages/agent_sandbox/shell/raw_client.py`
- `.venv/lib/python3.12/site-packages/agent_sandbox/file/raw_client.py`
- `.venv/lib/python3.12/site-packages/agent_sandbox/code/raw_client.py`
- `.venv/lib/python3.12/site-packages/agent_sandbox/jupyter/raw_client.py`

`nanorollout/envs/uda_env/ec2_runtime/uda_compat_server.py` implements the compatibility subset
needed by NanoRollout today. It can later be replaced by the full
agent-infra server if we want browser tabs, MCP, richer Jupyter sessions,
or stronger request validation without changing the EC2 runtime adapter.

### Computer Use

`POST /v1/computer-use/action` is aligned to Anthropic's computer-use
tool semantics and `ToolResult` shape:

```json
{
  "output": "string or null",
  "error": "string or null",
  "base64_image": "string or null"
}
```

The reference implementation for action semantics is Anthropic's
`computer-use-demo/computer_use_demo/tools` package. On EC2 we map these
actions onto Linux desktop primitives:

- screenshot: `scrot`, `gnome-screenshot`, then Python screenshot fallback
- mouse movement/click/drag: `xdotool`
- keyboard typing/hotkeys/key press: `xdotool`
- wait: server-side sleep

## Current EC2 Compatibility Level

The EC2 service is compatible with NanoRollout's current UDA call path:

- environment readiness through `/v1/sandbox`
- installed-agent shell execution through `/v1/shell/exec`
- task and artifact staging through `/v1/file/read` and `/v1/file/write`
- text edits through `/v1/file/str_replace_editor`
- simple Python execution through `/v1/code/execute`
- screenshot and basic desktop actions through `/v1/computer-use/action`

Known gaps against full upstream surfaces:

- Jupyter is currently a minimal session-create compatibility stub.
- Computer-use does not yet implement Anthropic's full coordinate scaling
  and validation logic; it matches the action names and result envelope
  required by NanoRollout.
- Browser-specific `/v1/browser/*` APIs are intentionally out of scope for
  the first EC2 runtime because UDA uses the computer-use desktop path.

## Acceptance Gates

Before an AMI can become the launch-template default:

- `GET /v1/sandbox` must be healthy on a fresh instance.
- `smoke_uda_ec2_runtime.py` must pass shell, file, code, and screenshot.
- `smoke_nanorollout_ec2_runtime.py` must launch, exercise SDK calls, and
  terminate the instance.
- `smoke_nanorollout_ec2_runtime.py --check-claude-code-install` must
  install Claude Code and report `claude --version`.
- A credentialed installed-agent rollout must write `trajectory.json`,
  `reward.json`, `metadata.json`, and agent-local artifacts, then pass
  `validate_installed_agent_rollout.py`. Claude Code and Codex CLI are
  both valid installed-agent gates; Claude remains the target path when
  Claude auth is available.
