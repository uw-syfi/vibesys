"""pi0.5 action chunk step as one megakernel on NVIDIA B300.

See .vibesys/tasks/vla-pi05-b300/OBJECTIVE.md.
"""


def build(case: dict):
    """Compile and allocate once; return run(inputs) -> outputs, one kernel launch per call."""
    raise NotImplementedError(case["id"])
