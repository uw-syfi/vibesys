"""Shared errors for the agent-policy strategies (multi, single, profile_multi, profile_single).

Each strategy previously defined its own byte-for-byte (or near-identical)
copy of these classes purely because its ``turns.py``/``session.py``/
``orchestration.py`` couldn't import a sibling strategy's module. They all
import this one shared module instead; strategies still never import each
other.
"""

from __future__ import annotations


class InvalidPlanError(ValueError):
    """The designer returned an invalid hypothesis state transition."""

    def __init__(self, detail: str) -> None:
        """Name the violated plan invariant."""
        super().__init__(detail)

    @classmethod
    def duplicate_updates(cls) -> InvalidPlanError:
        """Report repeated prior hypothesis IDs."""
        return cls("hypothesis_updates names one hypothesis more than once")

    @classmethod
    def self_reference(cls) -> InvalidPlanError:
        """Report a plan that updates its own new hypothesis."""
        return cls("hypothesis_updates includes the new hypothesis")

    @classmethod
    def reused_id(cls, hypothesis_id: str) -> InvalidPlanError:
        """Report a new hypothesis ID already used in this run."""
        return cls(f"hypothesis ID {hypothesis_id!r} was already used")


class PlanCorrectionExhaustedError(RuntimeError):
    """The plan correction loop ended without a validated plan."""

    def __init__(self) -> None:
        """Name the impossible correction state."""
        super().__init__("plan correction loop exited without a result")


class UnsupportedProfilerError(ValueError):
    """The selected domain cannot use the configured profiler."""

    def __init__(self) -> None:
        """Report missing Torch profiler support."""
        super().__init__("selected domain does not provide Torch profiler support")


class MissingImplementationError(ValueError):
    """A judge turn was requested without a parsed implementation."""

    def __init__(self) -> None:
        """Name the missing review input."""
        super().__init__("judge requires a parsed implementer response")


class RoleIsolationError(RuntimeError):
    """A role left unauthorized workspace changes after restoration.

    ``role`` defaults to ``"orchestrator"`` for strategies with a single
    isolated role (single, profile_single); strategies with more than one
    isolated role (multi, profile_multi) pass it explicitly.
    """

    def __init__(self, remaining: list[str], *, role: str = "orchestrator") -> None:
        """Name the role and paths that could not be isolated."""
        super().__init__(
            f"Cannot isolate {role}: workspace is still modified after restore: "
            + ", ".join(remaining[:8])
        )


class InvalidStrategyOptionsError(ValueError):
    """An option is incompatible with a strategy's orchestration identity."""

    def __init__(self, orchestration_id: str, field: str, value: object) -> None:
        """Name the strategy, the incompatible option, and its value."""
        super().__init__(f"{orchestration_id}: invalid {field} option {value!r}")


class StrategySessionError(RuntimeError):
    """Invalid session/round state detected by a strategy's session logic.

    Each strategy binds this class under its own name (``MultiSessionError``,
    ``SingleSessionError``, ...) so ``isinstance``/``pytest.raises`` checks by
    that name keep working; the class body is shared.
    """

    @classmethod
    def missing_active(cls) -> StrategySessionError:
        """Report a plan that failed to create an active hypothesis."""
        return cls("designer plan did not create an active hypothesis")

    @classmethod
    def missing_rollback(cls) -> StrategySessionError:
        """Report a rollback resolution that omitted a commit."""
        return cls("rollback resolution omitted a commit")

    @classmethod
    def exhausted_attempts(cls, round_number: int, first: int, limit: int) -> StrategySessionError:
        """Report a round that already exhausted its retry budget."""
        return cls(
            f"Round {round_number} already persisted {first - 1} attempts, "
            f"exhausting max_retries_per_round={limit}"
        )

    @classmethod
    def missing_gate_reason(cls) -> StrategySessionError:
        """Report an official gate requested without a reason."""
        return cls("official gate requested without a reason")

    @classmethod
    def missing_baseline(cls) -> StrategySessionError:
        """Report no available trusted baseline."""
        return cls("no trusted retained candidate or input baseline is available")

    @classmethod
    def missing_winner_commit(cls) -> StrategySessionError:
        """Report a selected candidate with no commit."""
        return cls("selected candidate has no commit")

    @classmethod
    def missing_implementation(cls) -> StrategySessionError:
        """Report a review requested without an implementer response."""
        return cls("review requires an implementer response")

    @classmethod
    def missing_profile_config(cls, detail: str) -> StrategySessionError:
        """Report missing profile-guided configuration.

        ``detail`` carries the strategy-specific wording (profile_single and
        profile_multi phrase this differently).
        """
        return cls(detail)
