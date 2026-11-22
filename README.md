# RLPipeline

Synthetic task generation for RL and evaluation of terminal agents. The pipeline
writes Harbor-format terminal tasks, checks them, measures how hard each one is
for a fixed agent, and keeps only the tasks in a learnable difficulty band.

## Purpose

The main use is to **author tasks with a larger model and teach a smaller model
to solve them**. A capable model such as Claude Opus 5.5 writes the environment,
the reference solution, the tests and the instruction. A smaller model such as
Claude Haiku 5.5 then attempts each task. The pipeline keeps only tasks where the
smaller model sometimes succeeds, 1 to 3 times in 5. Those are the tasks a model
can learn from: too easy teaches nothing, and too hard gives no signal.

Each shipped task therefore comes with evidence: which attempts passed, why each
failed attempt failed, and whether the task itself was fair. The shipped set can
be used for RL training, for evaluation, or for studying where a small model's
failures come from. A worked example is in [sample_run/](sample_run/).

A seed sets what a task is about. Two kinds of seed are supported:

- **Public seeds** (`--design public-seeds`): each task is seeded by a public
  Terminal-Bench 4.0 task. The public task is reverse-engineered once into a
  card (the skill it tests, what its verifier checks, where agents go wrong),
  and a new task is written from the card. See [docs/SEEDS.md](docs/SEEDS.md).
- **Taxonomy seeds** (the default): domain, task type, stack and skills drawn
  from `generator/taxonomy.py`, plus the optional design features described in
  [docs/DESIGN.md](docs/DESIGN.md).

## Pipeline

1. **Seed** the task (`generator/taxonomy.py`, `generator/public_seeds.py`).
2. **Author** the spec, environment, tests and instruction with the generator
   model (`generator/author.py`, prompts in `generator/prompts/`).
3. **Gate**: structural validation (`validator/validate.py`), a build check,
   an oracle run (the reference solution passes), a null-agent run (an empty
   container fails), and for public seeds a novelty check against the seed.
   Failed gates get a bounded number of repairs.
4. **Measure**: run the evaluation agent five times on the task.
5. **Ship** the task to `output/` only if 1 to 3 of 5 runs pass. Each shipped
   task has an evidence file saying why its failed runs failed.

Every model call, gate result, agent run and verdict is written under
`runs/<run_id>/`, so a shipped task can be checked from its records alone.
Interrupted batches resume from disk, and two copies of a batch can share one run id.

## Setup

Needs Docker running, Python 3.12, and Harbor.

```bash
uv tool install harbor                      # or: pip install harbor
python3.12 -m venv .venv
.venv/bin/pip install -r generator/requirements.txt pytest
cp .env.example .env                        # put your provider key in LLM_API_KEY
```

Only `.env` is read for credentials. Provider variables in the calling shell
are ignored, so a personal key is never sent to the wrong endpoint.

For public seeds, also run:

```bash
bash scripts/fetch_seeds.sh                 # clones Terminal-Bench 4.0 tasks into seeds/ (not committed)
.venv/bin/python -m generator.public_seeds build   # one card per public task
```

## Commands

```bash
bash scripts/generate.sh "fix an off-by-one in a Go pagination helper"   # one task, no measurement
bash scripts/validate.sh output/<task>                           # structural validator
bash scripts/run_batch.sh --run-id rl1 --n 10 --design public-seeds,early-build,full-reports
.venv/bin/python -m pytest tests -q                              # unit tests, no model or Docker
bash scripts/with_env.sh harbor run -p output/<task> -a oracle   # run Harbor with .env credentials
```

Batch options and design features are listed in [docs/DESIGN.md](docs/DESIGN.md).

## Layout

| Path | Contents |
|---|---|
| `generator/` | seeds, prompts, model calls, configuration |
| `pipeline/` | gates, build check, measurement, audits, records, report |
| `validator/` | structural validator for Harbor task directories |
| `scripts/` | batch, single-task, validation and credential wrappers |
| `tests/` | unit tests |
| `sample_run/` | an illustrative record of one seed through every stage |
| `output/` | shipped tasks (`output/<task>/`) and their evidence (`output/_evidence/`) |
| `runs/` | per-run records (not committed) |
| `seeds/` | fetched public tasks and their cards (not committed) |

## Fairness and reward-hacking checks

A shipped task has to measure the solver, not a mistake in the task or a
shortcut through its grader. Checks either reject a task, or flag it. A flagged
task still ships if its pass count is in the band, but it is marked unverified
and needs a human read.

**Before any agent runs**

- **Structural validation** (`validator/validate.py`): layout, `task.toml` schema and Dockerfile. Rejects, after one repair.
- **Oracle and null agent** (functional gate): the reference solution must pass, and an empty container must fail. Rejects, after bounded repairs.
- **Give-away scan** (`pipeline/quality.py`): the instruction or container must not state the answer or a pitfall. The instruction is rewritten once.
- **Answer keys in the image** (`pipeline/answer_keys.py`): no expected-output or golden file, and no generator script, may be left where the solver can read it. Rejects.
- **Novelty against a public seed** (`generator/public_seeds.py`): the instruction and tests may not repeat too many word pairs of the seed. Rejects after one repair.
- **Weakened tests on repair** (`generator/author.py`): a repair that removes or loosens an assertion is refused.

**After the agent runs**

- **Run audit** (`pipeline/analysis.py`): a model reads each attempt against the instruction. For a task in the band, a failure must be the solver's fault and a pass must be earned. A task that can be passed by cheating, or failed through no fault of the solver, is rejected. An audit that would reject is repeated twice, and the majority of three decides. A task nobody solved is called *broken* when at least three of five audits blame the task, and *too hard* otherwise. A task solved 4 or 5 times is rejected as too easy.
- **Fairness review** (`pipeline/run.py`, `pipeline/quality.py`): a model checks each test is fair to the instruction, and that its claim is stated in or inferable from the instruction and container.
- **Reward-hacking scan** (`pipeline/analysis.py`, `pipeline/flags.py`): every agent command is scanned for touching grading tools, grading files (`/opt/verifier`, `/logs/verifier`, `reward.txt`/`reward.json`), or test and solution files by name. A passing run that wrote to the grading files is rejected mechanically, whether by redirection, `tee`, copy, move, delete or in-place edit.

**Flags.** Audit and review findings become flags (`pipeline/flags.py`). Each names
its run. `output/_evidence/<task>/WHY_IT_FAILS.md` gives the cause of every failed
run of a shipped task, and whether the task is flagged.

**Limits.** The audits and reviews are model judgements, and earlier hand checks
found them disagreeing on like cases, so they do not replace a human read. The
give-away and novelty checks are lexical and miss paraphrase. The reward-hacking
scan works on commands, so an edit to a grader made by an unlisted route would
not be caught. The thresholds are not calibrated.

## Cautions

- Terminal-Bench tasks carry a canary asking that benchmark data stay out of
  training corpora. Keep Terminal-Bench 4 out of any training mix, and don't
  report its scores for a model trained on variants of its tasks.
- A task that passes the gates has not been read by a person. Hand-read a sample
  before using a batch.
- Evaluation depends on the agent and model named in `generator/config.py`.
  Changing them changes the difficulty band.

## License

Apache-2.0 (see `LICENSE`). Terminal-Bench, which provides the public seeds, is
also Apache-2.0; its tasks are fetched at build time and not redistributed here.
