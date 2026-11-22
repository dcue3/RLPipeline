# __CANARY__
"""Written by the task pipeline, not the task author.

pytest itself runs in a separate Python 3.13 so the tests do not depend on what
is installed in the task image. Without this file, that Python would also be
first on PATH for every program the tests launch, so `python3 script.py` would
run the solver's code on the wrong interpreter with none of the image's
packages. Restore the image's own environment for subprocesses.
"""

import os

_original_path = os.environ.get("TASK_ORIGINAL_PATH")
if _original_path:
    os.environ["PATH"] = _original_path
for _name in list(os.environ):
    if _name in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "TASK_ORIGINAL_PATH") or _name.startswith("UV_"):
        os.environ.pop(_name, None)
