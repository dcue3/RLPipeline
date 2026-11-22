You are revising the instruction for a terminal-based software engineering task. An AI coding agent will read it and then work in a Linux container; hidden tests decide whether it succeeded. The current instruction gives too much away.

Current instruction:
<<<INSTRUCTION
{instruction}
INSTRUCTION>>>

Sentences that give something away, and why:
{findings}

The hidden tests (so you keep everything they rely on):
```python
{tests}
```

Rewrite the instruction so that it contains two things only: the symptoms (what the person observes going wrong or missing) and the acceptance criteria (what a correct result must look like: the paths, formats, commands and exit codes the tests rely on).

It must NOT:
- say where documentation, configuration or any other source of the answer lives;
- name any defect or its cause, or say how many things are wrong;
- give advice or hints of any kind;
- explain how to solve the task, or mention the tests or the solution.

Keep the same voice and situation. Keep every acceptance criterion a test depends on that the solver could not find in the container. At most about 250 words, no headings.

Reply with the file only, in exactly this form, with nothing before or after:

<<<FILE instruction.md>>>
...the rewritten instruction...
<<<END>>>
