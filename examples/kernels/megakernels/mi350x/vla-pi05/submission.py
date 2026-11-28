"""pi0.5 action chunk step as one megakernel on AMD MI350X.

See .vibesys/tasks/vla-pi05-mi350x/OBJECTIVE.md.
"""


def build(case: dict):
    """Compile and allocate once; return run(inputs) -> outputs, one kernel launch per call."""
    raise NotImplementedError(case["id"])
