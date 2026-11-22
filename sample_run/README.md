# Sample run (illustrative)

This folder shows what the pipeline writes for one seed, from the seed card to the
shipping verdict. **The downstream records are invented** to show their formats and
the shipping rule. They are not output from a real run and are not measured results.

The one real file is `seed_card.json`: it is the card the pipeline produced with
Claude Opus 5.5 from the public Terminal-Bench 4.0 task `foodstuff-beta-activity`.
It paraphrases that task. It does not copy its instruction, tests or answer.

| Step | File | What it shows |
|---|---|---|
| 1. Reverse engineer a public task | `seed_card.json` | Skill, verifier, hard step, levers, and a template with parameters |
| 2. Draw a seed | `seed.json` | Card values and template parameters chosen for one task |
| 3. Author and gate | `task/`, `gates.json` | The generated instruction and tests, and the gate results for each version |
| 4. Measure | `band/runs.json` | Five agent runs of the evaluator (Claude Haiku 5.5), with pass or fail and the cause |
| 5. Audit and fairness | `analysis.json`, `fairness_review.json` | Whether each failure is the solver's fault, and whether each test is fair |
| 6. Verdict | `verdict.json`, `WHY_IT_FAILS.md`, `report.md` | Pass count, flags, and why each failed run failed |

In this example two of five runs pass, so the task lands in the 1 to 3 of 5 band
and ships with no flags.
