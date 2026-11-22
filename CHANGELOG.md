# Changelog

## Unreleased

### Re-skin as RLPipeline

- Replaced the project README and changelog with project-scope documentation.
- Removed the provided examples, the vendored Harbor source, the shipped tasks
  from the earlier pipeline work, and the earlier design documents. Earlier
  history is in git.
- Gateway settings renamed from the provider-specific names to `LLM_API_KEY` and
  `LLM_API_BASE`; the default endpoint is now OpenRouter.
- Task authorship in generated `task.toml` files is `RLPipeline`.
- Added the `public-seeds` design: public Terminal-Bench 4.0 tasks reverse-engineered
  into cards, a card-based seed draw, and a novelty gate (`docs/SEEDS.md`).

### Models, reverse engineering and seeds

- Generator model switched to Claude Opus 5.5 (`anthropic/claude-opus-5.5`); the
  evaluator is Claude Haiku 5.5 (`openrouter/anthropic/claude-haiku-5.5`).
- Cards gained template parameters and a skeleton, and `build --only` accepts any task directory.

### Removed the derive, visual, mechanisms and mix designs

- Deleted their seeds, prompts, gates, checks, reports fields and tests. The
  shared prompts and the `early-build`, `full-reports` and `public-seeds` designs are unchanged.
- Removed the generated-state check, which only the dropped designs used.
- Replaced the example-subject list with an empty `AVOID_SUBJECTS`.

### Not done

- No batch has been run under the `public-seeds` design. The novelty limits are uncalibrated.
