You study one public terminal-benchmark task and write a short reverse-engineered card for it. The card is used to write a NEW task that tests the same skill by the same mechanism, in a different scenario. Nothing you write may quote the public task.

Public task: {name}
Category: {category} / {subcategory}
Tags: {tags}
Base image: {base_image}

Instruction the agent sees:
<<<
{instruction}
>>>

Test files (what the verifier checks):
{tests}

Reference solution (excerpt):
<<<
{solution}
>>>

Write one JSON object with these keys:
- "domain": a short free-text label for the real-world area (for example "log forensics" or "payments reconciliation").
- "task_types": a list drawn from [{task_types}] that fits the task.
- "stack": one of [{stacks}], the closest to the environment.
- "skills": 2 or 3 short phrases, the skills a solver must genuinely use.
- "skill": one sentence on the core skill being tested.
- "verifier": one or two sentences on what the tests really check, including how the answer is judged (exact output, parsed state, behaviour, or a property).
- "hard_part": the specific step where a capable agent most often goes wrong, and why.
- "levers": at least two concrete ways a new variant could change difficulty while keeping the skill (for example more records, a trap in the data, a different format).
- "keep": what must stay the same in any variant so that it still tests the same mechanism.
- "change": at least two things that must differ from the public task (scenario, names, data, file layout, answer), so the variant is new.
- "template": an object with two parts. "parameters" is a list of at most three objects, each with "name" (a short identifier) and "values" (two to five concrete options). Each value must keep the skill and the hard step intact, so that a generator can vary the parameter to produce many distinct tasks from one skeleton. "skeleton" is one or two sentences describing the task with each parameter named in words, for example "a log of N services where the answer depends on the clock skew between hosts".

Rules: do not copy sentences, file names, identifiers or data values from the public task into any string you write. Describe mechanisms in your own words. Reply with the JSON object only.
