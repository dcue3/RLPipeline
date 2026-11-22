You design terminal-based software engineering tasks used to train and evaluate AI coding agents. An agent receives a written instruction, works in a Linux container through a shell, and hidden automated tests then decide whether it succeeded.

Propose {n_pitches} different task scenarios that fit this specification exactly:

{seed}

What makes a good scenario:
- It is a realistic situation the persona would actually face, with specific, concrete details (what the tool is, what data it handles, what is going wrong or missing).
- It genuinely needs every listed skill. Do not bolt skills on.
- A careful expert can finish it in 20-60 minutes, yet a strong solver fails it more often than not. That solver is not careless: it reads every file it is pointed at, knows the well-known gotchas (rounding, boundaries, encodings, exit codes in pipelines, reproducible archives) and tests its own work, so none of those make a task hard. What does: the truth has to be established by running things and observing, several plausible explanations have to be told apart, and a wrong early conclusion carries forward silently. Never vague or missing requirements, and nothing that depends on timing.
- Success is objectively checkable by a program: exact files, exact output, exact behaviour.
- It runs fully offline in a single small container with no GPU, no GUI and no external services.
- It is original. Do not base it on any existing benchmark task, tutorial or well-known exercise.

Avoid these subjects, which are already covered:
{avoid}

Make the {n_pitches} scenarios clearly different from one another in setting and in what is technically hard.

Reply with a JSON object only, in this form:
{{"pitches": [{{"title": "short title", "summary": "3-5 sentences: the situation, what exists in the container, what the agent must deliver", "what_is_hard": "1-2 sentences naming the specific trap or chain of reasoning that makes a hasty solver fail"}}]}}
