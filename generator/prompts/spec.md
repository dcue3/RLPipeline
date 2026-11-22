You design terminal-based software engineering tasks used to train and evaluate AI coding agents. An agent receives a written instruction, works in a Linux container through a shell, and hidden automated tests then decide whether it succeeded.

Write the full design specification for one task. Other engineers will build the container, the reference solution and the tests from this specification alone, so it must be precise and self-consistent.

Sampled specification:
{seed}

Chosen scenario:
Title: {pitch_title}
{pitch_summary}
What is hard: {pitch_hard}

Calibration. Read this carefully, because it decides whether the task is worth anything.

The solving agent is not a hasty engineer. It works at high reasoning effort, reads every file it is pointed at, and knows the standard gotchas: rounding modes, off-by-one boundaries, byte order, text encodings, deterministic archives, shell exit codes in pipelines, time zones. It tests its own work. A task whose difficulty is a list of such gotchas, or facts sitting in a short file the instruction names, is solved every time and is useless.

What this agent has actually been observed to get wrong:
- It builds a wrong picture of the starting state and then checks its work against that picture. In one observed task it had to reconstruct a position from raw data before analysing it; every run chose the right tools, but a run that misread two details went on to "verify" a wrong answer against its own misreading, and never noticed. Nothing in its process could have told it the reconstruction was wrong.
- It stops at the first answer that works when the task needs all of them, or the general one.
- It trusts a document, comment or name that describes the system when the system actually behaves differently.

So build difficulty from these, and only these:
- The truth has to be established, not looked up. The solver must run something in the container and observe the result, or measure a property of the data, or reproduce a failure, because reading alone does not settle it.
- There are several plausible places to look and several plausible explanations, and only investigation tells them apart.
- An early conclusion feeds later steps (depth), so a wrong one carries forward without any error message.
- Independent parts must all be right (width), and a solution that handles the examples at hand but not the general case fails.
- The stated difficulty source.

Never build difficulty from ambiguity, from anything the solver could not find out, or from anything that depends on timing. An expert who reads the instruction and investigates the container properly must be able to pass every test.

Design rules:
- Everything lives under /app unless the scenario needs a system path.
- The task must be fully deterministic: fixed dates, fixed random seeds, no dependence on the current time, on network content or on timing.
- Requirements must be individually testable by a program that only observes the final state of the container: files, their contents, and the behaviour of programs when run on inputs.
- Tests will also run the agent's work on fresh inputs the agent never saw, so a solution that hard-codes expected output must fail. Design requirements with that in mind.
- Keep it small. The starting container holds at most about 8 files and 400 lines in all (source, data, documents), and the reference solution is at most about 150 lines. Small tasks get built correctly and large ones do not. If the task needs data, a short generator script with a fixed seed may produce it at build time; it lives under `_build/` in the environment and is removed from the image, so the solver never sees it.
- Build the difficulty from conflicting evidence. The container contains one or two short pieces of material that describe the system wrongly or out of date (a changelog entry, a configuration file, a section of a README, a comment, a misleading name), while the truth is in what the data or the programs actually do. Nothing marks which source is right: the solver has to check by running something or examining the data, instead of trusting what is written. At least two of the pitfalls must be traps of this kind, and they must be independent, so that a solver can see through one and still fall for another. A fact a pitfall depends on must not sit in a file the instruction names.
- Each pitfall must be knowable from the container, never from the instruction. Its "knowable_from" must name a file in the container, the output of a command the solver can run there, or a behaviour the solver can observe there. "The instruction says ..." is not allowed: if a pitfall can only be known because the instruction mentions it, it is a hint, not a pitfall.
- When several things are wrong, what the persona observes must not map one-to-one onto the defects. One symptom may have two causes, and one cause may show up in several places.
- No answers in the container. Do not list among the environment files any expected-output, reference, golden or answer file, nor a complete working version of the program the solver must fix or build. The solver is told the required behaviour and has to produce it; hidden tests check it on the shipped data and on fresh inputs. Never design a task whose instruction is "make the output match this file".
- Requirements state WHAT must be observably true, never HOW to implement it. Do not name the command, option, function or algorithm the solver should use. Write "exits non-zero when any stage of the pipeline fails", not "uses `set -o pipefail`".
- The scenario describes the situation and its symptoms as the persona experiences them. It must not list the defects or their fixes. Working out what is wrong is the solver's job, and is where the difficulty should come from.
- Nothing may depend on timing, races, signal ordering or how long something takes to run: such requirements cannot be tested reliably. If the scenario involves concurrency or signals, define requirements whose outcome is the same on every run.
- Prefer the language's standard library. Do not rely on third-party packages that would have to be downloaded, except well-known ones installable with pip or apt.

Reply with a JSON object only, with these keys:
- "slug": 3-6 lowercase words joined by hyphens, naming the task
- "title": short title
- "description": one sentence describing what the task evaluates
- "category": one of "software-engineering", "system-administration", "data-processing", "security", "debugging", "scientific-computing"
- "tags": 2-4 lowercase tags
- "difficulty": "medium" or "hard"
- "expert_time_estimate_min": number
- "scenario": one paragraph describing the situation from the persona's point of view. This is shown to the solver, so it describes what the persona observes and needs, never the causes or the fixes
- "environment": list of {{"path": absolute path in the container, "purpose": what the file is and what it contains, in enough detail to write it}}
- "requirements": list of {{"id": "R1", "text": a precise, testable requirement}}. Between 4 and 6 items, no more. These are shown to the solver. They state required behaviour and may fix interfaces (paths, command names, formats, exit codes), but never name a defect or its fix
- "pitfalls": private design notes, never shown to the solver. List of {{"what": the specific thing a capable solver gets wrong, and why its own checks would not catch it, "knowable_from": the file, command output or observable behaviour in the container that reveals it (never the instruction), "requirements": [ids it affects]}}. 2 or 3 items, no more.
- "solution_outline": ordered list of the steps the reference solution takes
- "fresh_input_checks": list of short descriptions of new inputs the tests will construct to check behaviour beyond the files shipped in the container
