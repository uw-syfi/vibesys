"""Qwen3-0.6B decode step as one megakernel on AMD MI350X.

See .vibesys/tasks/dense-qwen3-0.6b-mi350x/OBJECTIVE.md.
"""


def build(case: dict):
    """Compile and allocate once; return run(inputs) -> outputs, one kernel launch per call."""
    raise NotImplementedError(case["id"])
