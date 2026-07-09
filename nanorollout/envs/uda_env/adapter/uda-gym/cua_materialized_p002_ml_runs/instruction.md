We need to lock the model for the support-assistant QA slot before Wednesday's deploy review, and I want the call made from the actual eval numbers, not vibes.

All the candidate runs are in our W&B workspace. The seeded dashboard is already open in Chrome; use that open browser session and go to the **`acme-nlp / qa-eval`** project. There are ten runs in there. Everything you need is on the run pages (state, F1, exact-match, p95 latency, $/1k cost, and which eval set each run used and how many examples) — pull it straight from the dashboard, run by run. Heads up: some runs crashed or are still going, and a couple of the flashiest F1 numbers weren't measured on the same eval set, so don't just sort by F1 and grab the top.

A run only counts as **deployable** if it clears *all* of these:

1. It finished cleanly — exclude anything still `running` or `crashed`.
2. It was scored on our official holdout, i.e. eval set `holdout-v2` with at least 5000 examples. A dev/smoke split (or a partial run that didn't get through the full holdout) does not count.
3. F1 ≥ 0.85 on that holdout.
4. p95 latency ≤ 250 ms.
5. Inference cost ≤ $0.80 per 1k queries.

Among the runs that clear all five, the one we deploy is the **highest F1**.

For every run that's *not* deployable, tag it with exactly one reason code, chosen by this precedence (check in this order, first failure wins): `status_crashed` / `status_running` → `eval_set_not_full_holdout` → `below_f1_threshold` → `latency_over_budget` → `cost_over_budget`.

Write two files to `/tmp_workspace/results/`:

1. **`run_evaluation.csv`** — one row per run (all ten), header exactly:
   `run_id,run_name,model,state,f1,em,p95_latency_ms,cost_per_1k_usd,eval_set,eval_n,deployable,exclusion_reason`
   - `deployable` is `true` or `false`.
   - `exclusion_reason` is the reason code for non-deployable runs, and empty for deployable ones.
   - For runs that never finished (so latency/cost were never measured), leave those numeric cells blank rather than guessing.

2. **`deployment_decision.json`** — an object with:
   - `recommended_run_id`, `recommended_run_name`, `recommended_model`, `recommended_f1`
   - `deployable_run_ids` — the deployable run IDs, sorted ascending
   - `deployable_count` — integer
   - `excluded` — an object mapping each reason code to the sorted list of run IDs excluded for that reason

No write-up or summary doc — just those two structured files. The deploy-review tooling reads them directly, so the column names, keys, and reason codes have to match exactly.
