# Design

## Stages

| Stage | Code | Output |
|---|---|---|
| Seed draw | `generator/taxonomy.py` (`sample_seed`), `generator/public_seeds.py` (`draw_card`) | `seed.json` per task |
| Pitch and spec | `generator/author.py`, `prompts/pitch*.md`, `prompts/spec*.md` | spec with requirements, pitfalls and entry point |
| Environment and tests | `prompts/environment*.md`, `prompts/tests*.md` | `task/` files, versioned as `v1/`, `v2/`, ... |
| Build check | `pipeline/build_check.py` | image builds or repair is attempted |
| Gates | `pipeline/gates.py`, `pipeline/candidate.py` | `gates/vN.json` |
| Measurement | `pipeline/evaluate.py`, `pipeline/harbor_runner.py` | `band/vN/aK.json`, five runs |
| Audit and flags | `pipeline/analysis.py`, `pipeline/quality.py`, `pipeline/flags.py` | `analysis/vN.json`, flags |
| Shipping and report | `pipeline/run.py`, `pipeline/report.py` | `output/`, `runs/<id>/report.md` |

## Gates

Checked cheapest first, stopping at the first failure:

1. **Structural**: layout and `task.toml` schema via `validator/validate.py`, plus static checks.
2. **Novelty** (public seeds only): the instruction and tests may not repeat too
   many word pairs of the seed (`NOVELTY_LIMITS` in `generator/config.py`).
3. **Functional**: the oracle (reference solution) must pass and the null agent must fail.

Each failure kind has its own repair allowance (`REPAIR_LIMITS`). A repair is
recorded as a new version, so earlier versions remain on disk.

## Shipping rule

A task ships if 1 to 3 of 5 measured runs pass. A run that failed for a reason
outside the task (timeout, gateway error, no Docker network) is replaced. A
passing run that wrote to grading files rejects the task. Audits and a fairness
review then attach flags; a flagged task is shipped but marked unverified.

## Records

```
runs/<run_id>/
  run.json            settings and the code commit
  events.jsonl        ledger of every action
  report.md           funnel, histogram, cost, per-task outcome
  tasks/<task_id>/    seed.json, state.json, llm/, vN/, gates/, band/, analysis/, jobs/
output/<task>/            shipped task files only
output/_evidence/<task>/  WHY_IT_FAILS.md and verdict.json
```

Rebuild the report from records at any time:

```bash
.venv/bin/python -m pipeline.report runs/<run_id>
```

## Batch options

| Option | Meaning |
|---|---|
| `--design` | Comma-separated design features (below). Default: none |
| `--workers` | Tasks in progress at once (default 10) |
| `--harbor-jobs` | Containers running at once (default 40) |
| `--start` | First seed index, to continue an earlier run |
| `--seed-key` | Draw the seeds of another run, to compare designs task for task |
| `--task-types` | Limit seeds to these task types |
| `--author-only` | Write tasks and run nothing; run again without it to check and measure |
| `--allow-dirty` | Start with uncommitted changes (otherwise every run is tied to a commit) |

## Design features

| Feature | Meaning |
|---|---|
| `early-build` | Build the image as soon as the container files exist; repair build failures before writing more on top |
| `full-reports` | Give a failed-oracle repair the solution's output and every failed test |
| `public-seeds` | Seed each task from a public Terminal-Bench 4.0 card (`docs/SEEDS.md`). Compatible with `early-build` and `full-reports` |
