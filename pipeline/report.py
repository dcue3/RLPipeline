"""Rebuild a run's metrics from its records.

    python -m pipeline.report runs/<run_id>

Everything here is read from the ledger and the per-task files, so the numbers
can be checked without reading the pipeline's code.
"""

import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from generator import config
from pipeline import flags
from pipeline.store import RunStore, atomic_write_json, atomic_write_text, read_json


def _seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _fmt_duration(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {sec:02d}s"


def collect(store: RunStore) -> dict:
    events = store.events()
    tasks = []
    for tdir in sorted(store.tasks_dir.iterdir()):
        if not tdir.is_dir():
            continue
        state = read_json(tdir / "state.json")
        if not state:
            continue
        task_id = tdir.name
        mine = [e for e in events if e.get("task_id") == task_id]
        llm_logs = [read_json(p) or {} for p in sorted((tdir / "llm").glob("*.json"))]
        gates = [read_json(p) or {} for p in sorted((tdir / "gates").glob("v*.json"))]
        passed_gates = {g["gate"] for record in gates for g in record.get("gates", []) if g.get("passed")}
        band = read_json(tdir / "band" / f"v{state.get('version')}" / "summary.json")
        confirm = read_json(tdir / "confirm" / f"v{state.get('version')}" / "summary.json")
        analysis = read_json(tdir / "analysis" / f"v{state.get('version')}.json")
        seed = read_json(tdir / "seed.json") or {}
        attempt_cost = sum(e.get("cost_usd") or 0 for e in mine if e["stage"] == "band_attempt")
        median_steps = _median_steps(tdir / "band" / f"v{state.get('version')}", band)
        first_gate = (gates[0].get("final") or {}) if gates else {}
        tasks.append(
            {
                "task_id": task_id,
                "slug": state.get("slug"),
                "final": state.get("final") or f"in progress ({state.get('status')})",
                "reason": state.get("reason"),
                "seed": {k: seed.get(k) for k in ("domain", "task_type", "stack", "depth", "width", "difficulty_source")},
                "versions": state.get("version", 0),
                "structural_ok": "structural" in passed_gates,
                "functional_ok": "functional" in passed_gates,
                "ready": state.get("status") == "ready",
                "passes": band["passes"] if band and band.get("complete") else None,
                "invalid_runs": band["invalid_runs"] if band else None,
                "median_steps": median_steps,
                # How authoring went: what the first version's gates said, and the replies thrown away.
                "first_check": None if not first_gate else ("passed" if first_gate.get("passed") else f"{first_gate.get('gate')}: {first_gate.get('reason')}"),
                "discarded_replies": dict(Counter(l.get("purpose") for l in llm_logs if l.get("usable") is False)),
                "calls_by_purpose": dict(Counter(l.get("purpose") for l in llm_logs)),
                "band": band["verdict"] if band else None,
                "confirm_passes": confirm["passes"] if confirm and confirm.get("complete") else None,
                "labels": analysis["labels"] if analysis else None,
                "fair": analysis["fair"] if analysis else None,
                "flags": state.get("flags") or [],
                "failed_runs": flags.failed_runs(analysis) if state.get("final") == "shipped" else [],
                "authoring_cost_usd": round(sum(l.get("cost_usd") or 0 for l in llm_logs), 4),
                "agent_cost_usd": round(attempt_cost, 4),
                "llm_calls": len(llm_logs),
                "seconds": round(_seconds(mine[0]["ts"], mine[-1]["ts"]), 0) if len(mine) > 1 else 0,
            }
        )

    stamps = [e["ts"] for e in events]
    started = [t for t in tasks]
    evaluated = [t for t in tasks if t["passes"] is not None]
    shipped = [t for t in tasks if t["final"] == "shipped"]
    learnable = [t for t in evaluated if t["band"] == "learnable"]
    authoring = sum(t["authoring_cost_usd"] for t in tasks)
    agent = sum(t["agent_cost_usd"] for t in tasks)
    lengths = [t["median_steps"] for t in evaluated if t["median_steps"] is not None]
    return {
        "run": read_json(store.run_dir / "run.json") or {"run_id": store.run_dir.name},
        "authoring": _authoring_summary(tasks),
        "run_length": {
            "tasks_measured": len(lengths),
            "median_of_task_medians": statistics.median(lengths) if lengths else None,
            "tasks_at_20_steps_or_more": sum(s >= 20 for s in lengths),
            "unsolved_and_short": [
                t["task_id"] for t in evaluated if t["passes"] == 0 and t["median_steps"] is not None and t["median_steps"] < config.SHORT_RUN_STEPS
            ],
        },
        "counts": {
            "candidates": len(started),
            "structurally_valid": sum(t["structural_ok"] for t in tasks),
            "functionally_correct": sum(t["functional_ok"] for t in tasks),
            "passed_all_gates": sum(t["ready"] for t in tasks),
            "evaluated": len(evaluated),
            "learnable": len(learnable),
            "shipped": len(shipped),
            "shipped_unflagged": sum(not t["flags"] for t in shipped),
            "in_progress": sum(t["final"].startswith("in progress") for t in tasks),
        },
        "yield_shipped_per_candidate": round(len(shipped) / len(started), 3) if started else 0,
        "yield_learnable_per_evaluated": round(len(learnable) / len(evaluated), 3) if evaluated else 0,
        "pass_histogram": {str(n): sum(t["passes"] == n for t in evaluated) for n in range(6)},
        "outcomes": dict(Counter(t["final"] for t in tasks)),
        "cost_usd": {
            "authoring": round(authoring, 2),
            "agent_runs": round(agent, 2),
            "total": round(authoring + agent, 2),
            "per_candidate": round((authoring + agent) / len(started), 2) if started else 0,
            "per_shipped": round((authoring + agent) / len(shipped), 2) if shipped else None,
        },
        "wall_clock_seconds": round(_seconds(min(stamps), max(stamps)), 0) if stamps else 0,
        "task_seconds_total": sum(t["seconds"] for t in tasks),
        "diversity": {
            axis: dict(Counter(t["seed"].get(axis) for t in shipped))
            for axis in ("domain", "task_type", "stack", "difficulty_source")
            if any(t["seed"].get(axis) is not None for t in shipped) or axis in ("domain", "task_type", "stack", "difficulty_source")
        },
        "tasks": tasks,
    }


def _median_steps(band_dir: Path, band: dict | None) -> float | None:
    """Median agent steps over the counted runs, read from the attempt files so older runs have it too."""
    if not band:
        return None
    if band.get("median_steps") is not None:
        return band["median_steps"]
    steps = []
    for number in band.get("counted_attempts") or []:
        trial = (read_json(band_dir / f"a{number}.json") or {}).get("trial") or {}
        if trial.get("n_episodes"):
            steps.append(trial["n_episodes"])
    return statistics.median(steps) if steps else None


def _authoring_summary(tasks: list[dict]) -> dict:
    """How the files came out before any repair: first functional check, and replies that were thrown away."""
    gated = [t for t in tasks if t.get("first_check")]
    calls, discarded = Counter(), Counter()
    for t in tasks:
        calls.update(t.get("calls_by_purpose") or {})
        discarded.update(t.get("discarded_replies") or {})
    return {
        "tasks_gated": len(gated),
        "first_check": dict(Counter(t["first_check"] for t in gated)),
        "calls": dict(calls),
        "discarded": dict(discarded),
    }


def _authoring_lines(authoring: dict) -> list[str]:
    if not authoring.get("calls"):
        return []
    lines = ["## Authoring", ""]
    if authoring["tasks_gated"]:
        first = authoring["first_check"]
        lines.append(
            f"- First version's gates, before any repair ({authoring['tasks_gated']} tasks): "
            + ", ".join(f"{name} {count}" for name, count in sorted(first.items(), key=lambda kv: (kv[0] != "passed", kv[0])))
        )
    thrown = authoring["discarded"]
    if thrown:
        lines.append(
            "- Replies thrown away (cut off or unusable), of calls made: "
            + ", ".join(f"{purpose} {count} of {authoring['calls'].get(purpose, 0)}" for purpose, count in sorted(thrown.items()))
        )
    else:
        lines.append("- No reply was thrown away.")
    return lines + [""]


def _run_length_lines(length: dict) -> list[str]:
    if not length.get("tasks_measured"):
        return []
    lines = [
        "## Run length",
        "",
        f"- Median agent steps per task (median of each task's median): {length['median_of_task_medians']:g}",
        f"- Tasks whose runs took 20 steps or more: {length['tasks_at_20_steps_or_more']} of {length['tasks_measured']}",
    ]
    if length.get("unsolved_and_short"):
        lines.append(
            f"- Solved by no run and under {config.SHORT_RUN_STEPS} steps, so probably broken, not hard: "
            + ", ".join(length["unsolved_and_short"])
        )
    return lines + [""]


def render(data: dict) -> str:
    c, cost = data["counts"], data["cost_usd"]
    lines = [
        f"# Run report: {data['run'].get('run_id')}",
        "",
        f"Generator `{data['run'].get('generator_model')}`; evaluator `{data['run'].get('eval_agent')}` on "
        f"`{data['run'].get('eval_model')}` at reasoning effort `{data['run'].get('eval_reasoning_effort')}`; "
        f"code commit `{data['run'].get('code_commit')}`"
        + (" (with uncommitted changes)" if data["run"].get("uncommitted_changes") else "") + ".",
        *(["", f"Change under test: {data['run']['change_under_test']}"] if data["run"].get("change_under_test") else []),
        *(["", f"Design features: {', '.join(data['run']['design']) or 'none (the b7 design)'}"] if "design" in data["run"] else []),
        "",
        "## Funnel",
        "",
        "| Stage | Tasks |",
        "|---|---|",
        f"| Candidates started | {c['candidates']} |",
        f"| Passed structural validation | {c['structurally_valid']} |",
        f"| Functionally correct (oracle passes twice, nop fails) | {c['functionally_correct']} |",
        f"| Ready for evaluation (structural and functional gates) | {c['passed_all_gates']} |",
        f"| Evaluated with 5 valid agent runs | {c['evaluated']} |",
        f"| In the band (1-3 of 5 passed) | {c['learnable']} |",
        f"| Shipped (in the band; the pass count decides) | {c['shipped']} |",
        f"| Shipped with no flags from the run audit or the fairness review | {c.get('shipped_unflagged', c['shipped'])} |",
        "",
        f"**Yield, in the band:** {c['shipped']} shipped of {c['candidates']} candidates "
        f"({data['yield_shipped_per_candidate']:.0%}); {c['learnable']} of {c['evaluated']} evaluated tasks landed in the band.",
        f"**Yield, in the band with no flags:** {c.get('shipped_unflagged', c['shipped'])} of {c['candidates']} candidates. "
        "A flag is a model's doubt about a failed run or a test. It does not stop a task shipping; a flagged task "
        "has not been verified and still needs a human read.",
        "",
        "## Pass distribution of evaluated tasks",
        "",
        "| Passes out of 5 | " + " | ".join(str(n) for n in range(6)) + " |",
        "|---|" + "---|" * 6,
        "| Tasks | " + " | ".join(str(data["pass_histogram"][str(n)]) for n in range(6)) + " |",
        "",
        "## Outcomes",
        "",
        "| Outcome | Tasks |",
        "|---|---|",
        *[f"| {name} | {count} |" for name, count in sorted(data["outcomes"].items())],
        "",
        "## Cost and time",
        "",
        f"- Total cost: ${cost['total']:.2f} (authoring and reviews ${cost['authoring']:.2f}, agent runs ${cost['agent_runs']:.2f})",
        f"- Per candidate: ${cost['per_candidate']:.2f}" + (f"; per shipped task: ${cost['per_shipped']:.2f}" if cost["per_shipped"] else ""),
        f"- Wall-clock time: {_fmt_duration(data['wall_clock_seconds'])}",
        "",
        *_run_length_lines(data.get("run_length") or {}),
        *_authoring_lines(data.get("authoring") or {}),
        "## Tasks",
        "",
        "Steps is the median number of agent steps over the counted runs.",
        "",
    ]
    # A second measurement was taken on early runs only; show its column only if some task has one.
    confirmed = any(t["confirm_passes"] is not None for t in data["tasks"])
    lines += [
        f"| Task | Seed | Versions | Passes | Steps | {'Second measurement | ' if confirmed else ''}Outcome | Flags | Cost | Reason |",
        "|---|---|---|---|---|---|---|---|---|" + ("---|" if confirmed else ""),
    ]
    for t in data["tasks"]:
        seed = t["seed"]
        lines.append(
            f"| {t['task_id']} {t['slug'] or ''} | {seed.get('domain')}, {seed.get('task_type')}, {seed.get('stack')}, "
            f"d{seed.get('depth')} w{seed.get('width')} | {t['versions']} | "
            f"{'' if t['passes'] is None else str(t['passes']) + '/5'} | "
            f"{'' if t.get('median_steps') is None else format(t['median_steps'], 'g')} | "
            + (f"{'' if t['confirm_passes'] is None else str(t['confirm_passes']) + '/5'} | " if confirmed else "")
            + f"{t['final']} | {len(t.get('flags') or []) if t['final'] == 'shipped' else ''} | "
            f"${t['authoring_cost_usd'] + t['agent_cost_usd']:.2f} | {(t['reason'] or '')[:110]} |"
        )
    shipped = [t for t in data["tasks"] if t["final"] == "shipped"]
    if shipped:
        flagged = [t for t in shipped if t.get("flags")]
        lines += ["", "## Flagged tasks: not verified, still need a human read", ""]
        if not flagged:
            lines.append("None of the shipped tasks carries a flag.")
        else:
            lines += [
                "These shipped because they landed in the band. The run audit or the fairness review raised a doubt",
                "about each; those are model judgements that have been wrong in both directions, so they flag a task",
                "for reading and do not reject it. Until someone has read them, do not present these as verified.",
                "",
            ]
        for t in flagged:
            lines.append(f"- **{t['slug']}** ({t['passes']}/5), {len(t['flags'])} flag(s):")
            lines += [f"  - {flags.describe(flag)[:420]}" for flag in t["flags"]]
        lines += ["", "## Why each shipped task fails", ""]
        for t in shipped:
            lines.append(f"### {t['slug']} ({t['passes']}/5{', flagged' if t.get('flags') else ''})")
            lines.append("")
            if not t.get("failed_runs"):
                lines.append("No run audit is on record for this task: its failed runs are not explained.")
            for run in t.get("failed_runs") or []:
                tests = ", ".join(run.get("failed_tests") or []) or "none recorded"
                lines.append(f"- Run {run['attempt']}, {run['label']}: {str(run.get('explanation') or '').strip()[:600]} (failed tests: {tests})")
            lines.append("")
        lines += ["", "## Diversity of shipped tasks", ""]
        for axis, counts in data["diversity"].items():
            lines.append(f"- {axis}: " + ", ".join(f"{k} ({v})" for k, v in sorted(counts.items(), key=lambda kv: str(kv[0]))))
    return "\n".join(lines) + "\n"


def write_report(store: RunStore) -> Path:
    data = collect(store)
    atomic_write_json(store.run_dir / "report.json", data)
    path = store.run_dir / "report.md"
    atomic_write_text(path, render(data))
    return path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m pipeline.report runs/<run_id>")
    print(write_report(RunStore(Path(sys.argv[1]))))
