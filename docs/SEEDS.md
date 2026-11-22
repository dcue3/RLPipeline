# Public seeds

## Source

`scripts/fetch_seeds.sh` shallow-clones `harbor-framework/terminal-bench` at tag
`v4.0.0` into `seeds/terminal-bench-4/`. That repository is Apache-2.0. Its task
files are benchmark data and are not committed here.

## Cards

`python -m generator.public_seeds build` asks the generator model once per public
task for a card, using `generator/prompts/seed_card.md`. A card holds:

| Key | Meaning |
|---|---|
| `domain`, `task_types`, `stack`, `skills` | Seed fields used by the sampler |
| `skill` | The core skill tested |
| `verifier` | What the tests check, and how the answer is judged |
| `hard_part` | Where a capable agent goes wrong |
| `levers` | Ways a variant can change difficulty |
| `keep` | What must stay the same |
| `change` | What must differ from the public task |

Cards are cached in `seeds/cards/` and never written to `output/`. Canary lines
are stripped from every excerpt before a model reads it.

## Templates

A card's `template` has `parameters` (at most three, each with two to five values)
and a `skeleton` sentence. A seed draws one value per parameter, and the author
is told the chosen values, so one card yields many distinct tasks that keep the
same skill and hard step.

## Any task, not only Terminal-Bench

`python -m generator.public_seeds build --only <path-to-task-dir>` reverse-engineers
any Harbor task directory. The card records the directory as its `source_dir`, and
the novelty gate reads the seed from there.

## Drawing

`draw_card(run_id, index)` orders cards by a hash of the run id and card name.
Each run therefore meets the public tasks in its own order, and a resumed run
keeps its order.

## Novelty gate

A candidate's `instruction.md` and `tests/test_outputs.py` are compared with the
seed's instruction and test source by word-pair overlap (`generator/similarity.py`).
A share above `NOVELTY_LIMITS` rejects the candidate, and one repair is allowed
with a message to rewrite the wording and change the scenario, names, data and
layout. The limits are not calibrated yet.

## Limits

- The gate measures wording overlap only. A variant can keep the seed's mechanism
  while sharing none of its words, which is the intent, but it can also be close
  in skill. Judge a batch by a hand read.
- Do not train on Terminal-Bench 4 or on variants of it if its scores are to
  remain a clean evaluation.
