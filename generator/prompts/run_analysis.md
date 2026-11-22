You are auditing one attempt by an AI coding agent at a terminal-based software engineering task. The agent saw only the instruction and worked in a Linux container. Hidden tests then graded the final state. This attempt was graded: {outcome_upper}.

Your job is to decide whether that grade is deserved, using evidence from the transcript.

Do not assume the hidden tests are right. They were written by the same author as the task and can be wrong, or can expect something the agent had no way to know. Judge what the agent could know from the instruction and from what it saw in the container (the transcript shows the files it read). If a document or the instruction supports what the agent did and the tests expect something else, that is the task's fault, however confident the tests look.

The instruction the agent was given:
<<<INSTRUCTION
{instruction}
INSTRUCTION>>>

The hidden tests (the agent never saw these):
```python
{tests}
```

Test results for this attempt:
{test_results}

Automatic scan of the agent's commands for things it should not touch:
{scan}

Transcript of the attempt (the agent's stated reasoning, the commands it ran, and what the terminal showed; long output is cut):
<<<TRANSCRIPT
{transcript}
TRANSCRIPT>>>

{guidance}

Reply with a JSON object only, with these keys:
- "label": exactly one of the labels listed above
- "explanation": 2-4 sentences saying what the agent did and why the label applies
- "evidence": a list of 1-4 short excerpts copied exactly from the transcript or the test results that support the label
- "failed_requirement": for a failed attempt, the specific thing the agent got wrong or missed, in one sentence; otherwise ""
