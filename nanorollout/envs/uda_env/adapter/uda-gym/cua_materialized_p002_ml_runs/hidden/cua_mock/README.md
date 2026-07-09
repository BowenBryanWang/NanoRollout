# Harness setup — wandb_mock (qa-eval)

Harness/verifier plumbing only. Do **not** surface any of this to the solving agent.

- Mock: `wandb_mock`
- Deployed base URL: `https://cua-gym-wandb.xlang.ai`
- Initial state: `wandb_mock_initial_state.json` (this directory) — schema-valid against
  `CUA-Gym/.claude/skills/mock_websites/schemas/wandb_mock.md`.

## Seed

Run `context/warmup.sh` from the VM after `context/` is staged at `/tmp_workspace/context`.
It generates a `sid`, writes `/tmp/task_web_sid`, POSTs the initial state to
`POST /post?sid=<sid>`, and writes `/tmp_workspace/context/cua_mock_session.json`.

The agent then opens the seeded dashboard (`https://cua-gym-wandb.xlang.ai`) and reads the
`acme-nlp / qa-eval` project's runs. No local copy of the run metrics is exposed to the agent.

## Verifier readback

`GET https://cua-gym-wandb.xlang.ai/go?sid=<sid>` returns `{initial_state, current_state,
state_diff}`. This is a read-only task (no mock mutation expected); the scored deliverables
are the local CSV + JSON under `/tmp_workspace/results/`.

## Ground truth (for verifier-gen; not for the agent)

Deployability gate (ALL must hold): `state == finished`; `eval_set == holdout-v2` and
`eval_n >= 5000`; `f1 >= 0.85`; `p95_latency_ms <= 250`; `cost_per_1k_usd <= 0.80`.
Exclusion reason precedence: status -> eval_set -> f1 -> latency -> cost.

| run_id | name | deployable | exclusion_reason |
|--------|------|-----------|------------------|
| run-1 | amber-roberta-1 | false | latency_over_budget |
| run-2 | brisk-distil-2 | true | |
| run-3 | calm-minilm-3 | true | |
| run-4 | dawn-electra-4 | true (RECOMMENDED, f1 0.889) | |
| run-5 | eager-deberta-5 | false | eval_set_not_full_holdout |
| run-6 | frost-tinybert-6 | false | below_f1_threshold |
| run-7 | gilded-roberta-7 | false | status_crashed |
| run-8 | hazel-electra-8 | false | status_running |
| run-9 | ivory-distil-9 | false | cost_over_budget |
| run-10 | jade-minilm-10 | true | |

Deployable set = {run-2, run-3, run-4, run-10} (4). Recommended = run-4 (electra-base, f1 0.889).
