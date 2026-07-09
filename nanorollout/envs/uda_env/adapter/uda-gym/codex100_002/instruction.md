We need the candidate run pick for the `churn-retention-v3` experiment cleaned up before the model review.

The seeded W&B dashboard for this review is already open in Chrome. Compare that dashboard with the local exports under `/tmp_workspace/context/exports/`. The CSV export was pulled before the last run status refresh, so treat the dashboard as the source of truth for whether a run is actually finished. The QA files are the source of truth for data drift and split-health eligibility.

Save `/tmp_workspace/results/model_selection.json` with:

- `project`: string
- `selected_run_id`: string
- `baseline_run_id`: string
- `eligible_run_ids`: array of strings sorted by descending validation F1, then run id
- `excluded_runs`: array of objects with `run_id` and `reason_code`
- `metric_deltas`: object with `selected_vs_baseline_f1` and `selected_vs_baseline_latency_ms`

Use reason codes from this set only: `not_finished`, `qa_drift_fail`, `split_health_fail`, `metric_mismatch`, `not_candidate`.
