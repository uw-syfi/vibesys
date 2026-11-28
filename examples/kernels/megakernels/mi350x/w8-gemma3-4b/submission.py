"""Gemma 3 4B with 8-bit weights decode step as one megakernel on AMD MI350X.

See .vibesys/tasks/w8-gemma3-4b-mi350x/OBJECTIVE.md.
"""


def build(case: dict):
    """Compile and allocate once; return run(inputs) -> outputs, one kernel launch per call."""
    raise NotImplementedError(case["id"])
