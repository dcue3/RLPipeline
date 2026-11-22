"""Undo gate results that were caused by the machine, not by the tasks.

    python -m pipeline.reset_gates <run_id> --since 2026-11-17T07:10 --reason "Docker's disk was full"

When something outside the tasks makes every check fail (a full Docker disk,
for example) and it was not recognised as infrastructure at the time, tasks
end up with failed gate results, repairs they did not need, and rejections.
This puts every task that was gated since a given time back to the version it
had before: gate results, repaired versions and Harbor jobs written since then
are moved to ``discarded/<time>/`` in the task's records (nothing is deleted),
the task's state goes back to "authored", and the reset is written to the
ledger. Running the batch again then gates those tasks afresh.

Tasks that have been shipped are never touched.
"""

import argparse
import shutil
import sys
from datetime import datetime, timezone

from generator import config
from pipeline.store import RunStore, atomic_write_json, file_lock, now_iso, read_json, to_record_path

GATED_STAGES = ("structural", "functional", "repair", "reject", "infrastructure")


def _since_timestamp(since: str) -> float:
    moment = datetime.fromisoformat(since)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def reset_task(store: RunStore, task_id: str, since: str, reason: str) -> dict:
    """Put one task back to how it stood before ``since``. Returns what was done."""
    tdir = store.task_dir(task_id)
    cutoff = _since_timestamp(since)
    with file_lock(tdir / ".lock", blocking=False) as held:
        if not held:
            return {"task_id": task_id, "reset": False, "why": "another process is working on it"}
        state_path = tdir / "state.json"
        state = read_json(state_path) or {}
        if state.get("final") == "shipped":
            return {"task_id": task_id, "reset": False, "why": "shipped"}

        new_gates = [p for p in sorted((tdir / "gates").glob("v*.json")) if p.stat().st_mtime >= cutoff]
        version_dirs = sorted((d for d in tdir.glob("v[0-9]*") if d.is_dir()), key=lambda d: int(d.name[1:]))
        new_versions = [d for d in version_dirs if d.stat().st_mtime >= cutoff and int(d.name[1:]) > 1]
        kept = [d for d in version_dirs if d not in new_versions]
        new_jobs = [d for d in (tdir / "jobs").glob("*") if d.stat().st_mtime >= cutoff] if (tdir / "jobs").is_dir() else []
        if not (new_gates or new_versions) or not kept:
            return {"task_id": task_id, "reset": False, "why": "nothing gated since then"}

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bin_dir = tdir / "discarded" / stamp
        for item in [*new_gates, *new_versions, *new_jobs]:
            target = bin_dir / item.relative_to(tdir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(target))

        version = int(kept[-1].name[1:])
        task_dirs = [d for d in kept[-1].iterdir() if d.is_dir()]
        previous = {k: state.get(k) for k in ("status", "final", "version", "reason")}
        for key in ("final", "finished_at", "reason", "rejected_at", "limit", "passes", "confirm_passes"):
            state.pop(key, None)
        state.update(status="authored", version=version, task_path=to_record_path(task_dirs[0]) if task_dirs else state.get("task_path"))
        note = {"at": now_iso(), "since": since, "reason": reason, "previous": previous, "discarded": to_record_path(bin_dir)}
        state.setdefault("resets", []).append(note)
        atomic_write_json(state_path, state)
        store.log_event(
            task_id, "reset", "authored", version=version, since=since, reason=reason, previous=previous,
            gates_discarded=len(new_gates), versions_discarded=len(new_versions), evidence=to_record_path(bin_dir),
        )
        return {"task_id": task_id, "reset": True, "version": version, "previous": previous}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_id")
    parser.add_argument("--since", required=True, help="ISO time (UTC unless it says otherwise); gate results from then on are undone")
    parser.add_argument("--reason", required=True, help="what went wrong with the machine, for the record")
    args = parser.parse_args()
    store = RunStore(config.RUNS_DIR / args.run_id)
    if not store.tasks_dir.is_dir():
        sys.exit(f"No such run: runs/{args.run_id}")
    touched = sorted({e["task_id"] for e in store.events() if e.get("task_id") and e["stage"] in GATED_STAGES and e["ts"] >= args.since})
    done = [reset_task(store, task_id, args.since, args.reason) for task_id in touched]
    for result in done:
        was = result.get("previous") or {}
        print(f"{result['task_id']}: " + (f"reset to v{result['version']} (was {was.get('final') or was.get('status')} at v{was.get('version')})" if result["reset"] else f"left alone ({result['why']})"))
    print(f"{sum(r['reset'] for r in done)} of {len(done)} tasks reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
