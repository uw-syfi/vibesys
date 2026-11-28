"""Qwen3-30B-A3B decode step as one megakernel on AWS Trainium2.

See .vibesys/tasks/moe-qwen3-30b-a3b-trn2/OBJECTIVE.md.
"""


def build(case: dict):
    """Compile and allocate once; return run(inputs) -> outputs, one kernel launch per call."""
    raise NotImplementedError(case["id"])
