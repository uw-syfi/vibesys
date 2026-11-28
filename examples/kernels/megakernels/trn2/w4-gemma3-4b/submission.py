"""Gemma 3 4B with 4-bit weights decode step as one megakernel on AWS Trainium2.

See .vibesys/tasks/w4-gemma3-4b-trn2/OBJECTIVE.md.
"""


def build(case: dict):
    """Compile and allocate once; return run(inputs) -> outputs, one kernel launch per call."""
    raise NotImplementedError(case["id"])
