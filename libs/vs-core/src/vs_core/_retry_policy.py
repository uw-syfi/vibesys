"""Pure retry timing, shared by the generated retry declaration."""


class RequestRetryPolicy:
    """Behavior of a bounded exponential retry declaration."""

    __slots__ = ()

    backoff: float
    backoff_cap: float

    def delay(self, retries: int) -> float:
        """Core seconds before attempt number retries + 1, doubling to the cap."""
        return min(self.backoff * 2 ** max(retries - 1, 0), self.backoff_cap)
