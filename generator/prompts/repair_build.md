You are repairing the container of a terminal-based software engineering task. Its image did not build or did not start, so nothing else about the task has run yet.

Design specification (the source of truth for what the container should hold):
```json
{spec}
```

The container's files:
{files}

What failed:
{failure}

Find the cause in the build output and fix it in the container's files. Usual causes: a `COPY` of a file that does not exist; a `RUN` that calls an interpreter or compiler the base image does not have (slim images have no compiler, no `git` and often no `python3`; `gcc` needs the libc headers); a generator that raises, or whose own assertion about its output fails; a package that cannot be installed; a hand-written lock or checksum file.

Rules:
- Change only files under `environment/`. The instruction, the solution and the tests are not shown and must stay as they are, so do not change any path, file name, format or behaviour that the specification gives: fix how the container is built, not what it contains.
- Keep generators under `environment/_build/`, copied with `COPY _build/ /tmp/_build/` and run from `/tmp/_build/`. Do not delete that directory yourself and do not use `COPY . /app`.
- Do not add any file that holds expected results, and do not set an ENTRYPOINT or CMD. The last `WORKDIR` must be `/app`.

Reply with only the files that need to change, each complete, in blocks of exactly this form, with nothing before the first block or after the last:

<<<FILE environment/Dockerfile>>>
...full new file content...
<<<END>>>
