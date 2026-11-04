"""The input-check run's launch contract."""

from vibesys.metrics import MetricSpace
from vs_core.api.search import Value


class InputCheckLaunch(Value):
    """Task comparison facts needed to decode the input benchmark."""

    metric_space: MetricSpace


__all__ = ["InputCheckLaunch"]
