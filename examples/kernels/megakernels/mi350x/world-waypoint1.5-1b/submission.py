"""Waypoint-1.5-1B latent frame step as one megakernel on AMD MI350X.

See .vibesys/tasks/world-waypoint1.5-1b-mi350x/OBJECTIVE.md.
"""


def build(case: dict):
    """Compile and allocate once; return run(inputs) -> outputs, one kernel launch per call."""
    raise NotImplementedError(case["id"])
