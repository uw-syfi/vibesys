"""Qwen3-0.6B decode step as one megakernel on AWS Trainium2.

See .vibesys/tasks/dense-qwen3-0.6b-trn2/OBJECTIVE.md.
"""


def build(case: dict):
    """Compile and allocate once; return run(inputs) -> outputs, one kernel launch per call."""
    raise NotImplementedError(case["id"])
