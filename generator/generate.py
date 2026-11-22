"""Generate one Harbor task from a topic string.

    python -m generator.generate "fix an off-by-one in a Go pagination helper"

This is the single-task entry point: it authors the task, runs the structural
validator and the oracle/nop checks (with one repair for a structural failure
and one for a functional failure), and prints where the task was written,
which is under runs/<run_id>/tasks/, not output/. It does not measure
difficulty; the batch pipeline (pipeline/run.py) does that and decides what
is shipped to output/.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):  # allow `python generator/generate.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generator import config  # noqa: E402
from generator.llm import LLM  # noqa: E402
from generator.taxonomy import sample_seed  # noqa: E402
from pipeline.candidate import build_candidate  # noqa: E402
from pipeline.run import AVOID_SUBJECTS  # noqa: E402
from pipeline.store import RunStore, from_record_path  # noqa: E402


def generate_task(
    topic: str | None = None,
    *,
    run_id: str | None = None,
    index: int = 0,
    check: bool = True,
) -> dict:
    """Generate a Harbor task.

    Args:
        topic: Optional short description of the task to generate. When given it
            replaces the scenario pitch step; the rest of the spec seed (domain,
            skills, depth, width ...) is still sampled.
        run_id: Run directory name under runs/. Defaults to a timestamped one.
        index: Which seed of the run to draw.
        check: Run the structural and oracle/nop gates (needs Docker).

    Returns:
        The task state: at least {"task_dir": <path or None>, "status": <str>}.
    """
    run_id = run_id or datetime.now().strftime("adhoc-%Y%m%d-%H%M%S")
    store = RunStore(config.RUNS_DIR / run_id)
    seed = sample_seed(run_id, index, topic=topic)
    state = build_candidate(store, f"t{index:03d}", seed, LLM(), list(AVOID_SUBJECTS), run_gates=check)
    state["task_dir"] = str(from_record_path(state["task_path"])) if state.get("task_path") else None
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("topic", nargs="?", help="what the task should be about (optional)")
    parser.add_argument("--run-id", help="run directory under runs/ (default: adhoc-<timestamp>)")
    parser.add_argument("--index", type=int, default=0, help="seed index within the run")
    parser.add_argument("--no-check", action="store_true", help="skip the oracle/nop checks")
    args = parser.parse_args()

    state = generate_task(args.topic, run_id=args.run_id, index=args.index, check=not args.no_check)
    print(f"Status: {state['status']}" + (f" ({state.get('reason')})" if state.get("reason") else ""))
    print(f"Versions written: {state.get('version')}, authoring cost: ${state.get('llm_cost_usd', 0):.3f}")
    if state.get("task_dir"):
        print(f"Generated task at: {state['task_dir']}")
    return 0 if state["status"] in ("ready", "authored") else 1


if __name__ == "__main__":
    sys.exit(main())
