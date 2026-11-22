You are reviewing the design specification of a terminal-based software engineering task before anyone builds it. The task will be given to an AI coding agent as a written instruction; hidden automated tests then decide whether it succeeded. Your job is to find real defects in the design. Be strict and specific: a defect you miss wastes everything built on this specification.

What the task was supposed to be (sampled before the specification was written):
{seed}

The specification under review:
```json
{spec}
```

Answer every part with evidence taken from the specification. Do not approve something because it sounds reasonable; check it.

Reply with a JSON object only, with these keys:

- "skills": one entry per skill listed in the sampled specification: {{"skill": the skill, "requirement_ids": ids of requirements that cannot be met without it, "needed": true or false}}. "needed" is false if a solver could meet every requirement without exercising that skill.

- "depth_chain": the ordered list of steps a solver must take where each step depends on the result of the one before. Only include genuinely dependent steps.

- "requirements": one entry per requirement: {{"id": id, "behavioural": true or false, "testable": true or false, "deterministic": true or false, "issue": "" or what is wrong}}.
  - "behavioural" is false if the requirement prescribes HOW to implement something (a particular command, option, function, algorithm or code structure) instead of WHAT must be observably true. Example of a non-behavioural requirement: "the script must use `set -o pipefail`". Its behavioural form: "the script exits non-zero when any stage of the pipeline fails".
    Naming an interface is NOT prescribing an implementation. A requirement may fix the things a user or a test has to call or read: file paths, command names and arguments, a Makefile target name, a function or endpoint name, output formats and field names, exit codes. Those are behavioural. Only mark a requirement non-behavioural when it dictates the internals behind the interface.
  - "testable" is false if a program observing only the final container state (files, and the behaviour of programs run on inputs) cannot decide it.
  - "deterministic" is false if checking it depends on timing, races, signal delivery order, the current time, or anything that could differ between two runs.

- "contradictions": list of {{"ids": [ids], "problem": text}} for requirements that conflict with each other or with the described environment. Empty list if none.

- "pitfalls": one entry per pitfall: {{"index": position starting at 1, "knowable": true or false, "why": text}}. "knowable" is false if a careful expert reading only the instruction and exploring the container could not find out what is needed.

- "gives_away_solution": judge ONLY the "scenario" and "requirements" fields, which are what the solver will be shown. The "pitfalls", "solution_outline" and "environment" fields are private design notes the solver never sees, and are expected to describe the defects. Answer true only if the scenario or requirements name the cause of a defect or the fix for it (for example "the parser uses the wrong byte order", "add an index on user_id"), so that diagnosis is no longer needed. Stating the required behaviour precisely, however detailed, is not giving the solution away. Otherwise false.
- "gives_away_evidence": if true, quote the sentence that does it; otherwise "".

- "realistic": true if this is a situation the persona would plausibly face and the task hangs together as one piece of work; false if it is contrived or a list of unrelated chores.

- "problems": a list of concrete, actionable changes needed to fix every defect you found above. Empty list if the specification is sound.
