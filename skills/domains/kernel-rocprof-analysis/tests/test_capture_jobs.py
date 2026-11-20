"""The job forms in `references/capture.md` are accepted by `vibesys-cli`.

A ROCm task needs GPUs, and `vibesys-cli` refuses a check or command of a GPU task that does
not say how many GPUs it takes (`--gpus N`) wherever a placement declares a pool, so a capture
recipe that omits it fails when an agent copies it.
"""

from __future__ import annotations

import re
from pathlib import Path

CAPTURE = Path(__file__).resolve().parents[1] / "references" / "capture.md"

# A job form: the text after "submit command <sha>" to the end of its code span or its line.
_JOB_FORM = re.compile(r"submit command <sha>[^`\n]*")


def test_the_capture_recipe_has_job_forms() -> None:
    assert _JOB_FORM.findall(CAPTURE.read_text(encoding="utf-8"))


def test_every_job_form_says_how_many_gpus_it_takes() -> None:
    forms = _JOB_FORM.findall(CAPTURE.read_text(encoding="utf-8"))

    missing = [form for form in forms if "--gpus" not in form]

    assert not missing, f"job forms in capture.md without --gpus: {missing}"
