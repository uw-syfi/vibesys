"""Correctness generation, oracle evaluation, replay, and shrinking."""

from __future__ import annotations

import random as random_module
import string
import uuid as uuid_module
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vs_correctness.models import (
    ActionResult,
    CaseResult,
    Decision,
    Environment,
    Observation,
    OracleContext,
    TestCase,
    Verdict,
    VerificationReport,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(slots=True)
class GenerationContext:
    """Reproducible inputs supplied to a user generator."""

    seed: int
    cases: int
    _random: random_module.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize the deterministic, non-cryptographic generator."""
        self._random = random_module.Random(self.seed)  # noqa: S311

    def random(self) -> random_module.Random:
        """Return a fresh PRNG at this context's initial state."""
        return random_module.Random(self.seed)  # noqa: S311

    def integer(self, minimum: int, maximum: int) -> int:
        """Generate an inclusive bounded integer."""
        return self._rng.randint(minimum, maximum)

    def choice[T](self, values: tuple[T, ...] | list[T]) -> T:
        """Choose one value from a nonempty sequence."""
        return self._rng.choice(values)

    def boolean(self) -> bool:
        """Generate a boolean."""
        return bool(self._rng.getrandbits(1))

    def string(self, *, min_length: int = 0, max_length: int = 32) -> str:
        """Generate a printable identifier-safe string."""
        if min_length < 0 or max_length < min_length:
            raise ValueError("string length bounds are invalid")  # noqa: TRY003
        length = self.integer(min_length, max_length)
        alphabet = string.ascii_letters + string.digits + "_-"
        return "".join(self._rng.choice(alphabet) for _ in range(length))

    def uuid(self) -> uuid_module.UUID:
        """Generate a deterministic RFC 4122 UUID."""
        return uuid_module.UUID(int=self._rng.getrandbits(128), version=4)

    @property
    def _rng(self) -> random_module.Random:
        return self._random


class Generator(Protocol):
    """User extension point that creates executable fuzz inputs only."""

    def generate(self, context: GenerationContext) -> Iterable[TestCase]:
        """Generate at most `context.cases` cases."""
        ...


class Oracle(Protocol):
    """User extension point that derives absolute or differential correctness."""

    def check(self, context: OracleContext) -> Decision:
        """Return one explicit verdict."""
        ...


class Executor(Protocol):
    """Protocol adapter extension point. The built-in implementation is HTTP."""

    def execute(self, test_case: TestCase, environment: Environment) -> Observation:
        """Execute a case against one environment."""
        ...


@dataclass(frozen=True, slots=True)
class Suite:
    """A user's generation and correctness definition."""

    generator: Generator
    oracle: Oracle


type Reset = Callable[[Environment], None]


class Verifier:
    """Run identical serialized cases against candidate and optional baseline."""

    def __init__(
        self,
        executor: Executor,
        *,
        reset: Reset | None = None,
        max_shrink_attempts: int = 32,
    ) -> None:
        """Configure execution, fresh-state reset, and shrink budget."""
        if max_shrink_attempts < 0:
            raise ValueError("max_shrink_attempts must not be negative")  # noqa: TRY003
        self._executor = executor
        self._reset = reset
        self._max_shrink_attempts = max_shrink_attempts

    def verify(
        self,
        suite: Suite,
        *,
        candidate: Environment,
        baseline: Environment | None = None,
        seed: int = 0,
        cases: int = 100,
    ) -> VerificationReport:
        """Generate then verify a bounded, nonempty suite."""
        if cases <= 0:
            raise ValueError("cases must be positive")  # noqa: TRY003
        generated = list(
            islice(
                suite.generator.generate(GenerationContext(seed=seed, cases=cases)),
                cases + 1,
            )
        )
        if not generated:
            raise ValueError("generator produced no test cases")  # noqa: TRY003
        if len(generated) > cases:
            raise ValueError(  # noqa: TRY003
                f"generator produced {len(generated)} cases, limit is {cases}"
            )
        ids = [test_case.id for test_case in generated]
        if len(set(ids)) != len(ids):
            raise ValueError("generator produced duplicate test case ids")  # noqa: TRY003
        results = tuple(
            self._evaluate_and_shrink(
                test_case, suite.oracle, candidate=candidate, baseline=baseline
            )
            for test_case in generated
        )
        return VerificationReport(
            seed=seed, candidate=candidate, baseline=baseline, results=results
        )

    def replay(
        self,
        test_case: TestCase,
        oracle: Oracle,
        *,
        candidate: Environment,
        baseline: Environment | None = None,
    ) -> CaseResult:
        """Execute one serialized case without generating or shrinking it."""
        return self._evaluate(test_case, oracle, candidate=candidate, baseline=baseline)

    def _evaluate_and_shrink(
        self,
        test_case: TestCase,
        oracle: Oracle,
        *,
        candidate: Environment,
        baseline: Environment | None,
    ) -> CaseResult:
        result = self._evaluate(test_case, oracle, candidate=candidate, baseline=baseline)
        if result.decision.verdict is not Verdict.FAIL or self._max_shrink_attempts == 0:
            return result
        minimized, attempts = self._shrink(
            test_case, oracle, candidate=candidate, baseline=baseline
        )
        return result.model_copy(update={"minimized_case": minimized, "shrink_attempts": attempts})

    def _evaluate(
        self,
        test_case: TestCase,
        oracle: Oracle,
        *,
        candidate: Environment,
        baseline: Environment | None,
    ) -> CaseResult:
        baseline_observation = (
            self._execute_fresh(test_case, baseline) if baseline is not None else None
        )
        candidate_observation = self._execute_fresh(test_case, candidate)
        infrastructure_errors = []
        if baseline_observation is not None and baseline_observation.environment != baseline:
            infrastructure_errors.append("baseline observation identifies the wrong environment")
        if candidate_observation.environment != candidate:
            infrastructure_errors.append("candidate observation identifies the wrong environment")
        if baseline_observation is not None and baseline_observation.failed:
            infrastructure_errors.append("baseline execution failed")
        if candidate_observation.failed:
            infrastructure_errors.append("candidate execution failed")
        if (
            baseline_observation is not None
            and not baseline_observation.failed
            and (error := self._observation_alignment_error(test_case, baseline_observation))
        ):
            infrastructure_errors.append(f"baseline {error}")
        if not candidate_observation.failed and (
            error := self._observation_alignment_error(test_case, candidate_observation)
        ):
            infrastructure_errors.append(f"candidate {error}")
        if infrastructure_errors:
            decision = Decision(
                verdict=Verdict.INCONCLUSIVE, reason="; ".join(infrastructure_errors)
            )
        else:
            context = OracleContext(
                test_case=test_case,
                baseline=baseline_observation,
                candidate=candidate_observation,
            )
            try:
                decision = oracle.check(context)
                if not isinstance(decision, Decision):
                    raise TypeError("oracle must return Decision")  # noqa: TRY003, TRY301
            except Exception as error:  # noqa: BLE001
                decision = Decision(
                    verdict=Verdict.INCONCLUSIVE,
                    reason=f"oracle raised {type(error).__name__}: {error}",
                )
        return CaseResult(
            test_case=test_case,
            decision=decision,
            baseline=baseline_observation,
            candidate=candidate_observation,
        )

    @staticmethod
    def _observation_alignment_error(test_case: TestCase, observation: Observation) -> str | None:
        """Reject successful observations that do not map exactly to declared inputs."""
        expected = tuple(
            (phase, index)
            for phase, actions in (
                ("setup", test_case.setup),
                ("actions", test_case.actions),
                ("cleanup", test_case.cleanup),
            )
            for index in range(len(actions))
        )
        actual = tuple((result.phase, result.index) for result in observation.action_results)
        if actual != expected:
            return f"observation result positions {actual!r} do not match inputs {expected!r}"
        return None

    def _execute_fresh(self, test_case: TestCase, environment: Environment) -> Observation:
        try:
            if self._reset is not None:
                self._reset(environment)
            return self._executor.execute(test_case, environment)
        except Exception as error:  # noqa: BLE001
            return Observation(
                environment=environment,
                action_results=(
                    ActionResult(
                        phase="setup",
                        index=0,
                        error=f"executor raised {type(error).__name__}: {error}",
                    ),
                ),
            )

    def _shrink(
        self,
        original: TestCase,
        oracle: Oracle,
        *,
        candidate: Environment,
        baseline: Environment | None,
    ) -> tuple[TestCase, int]:
        current = original
        attempts = 0
        chunk = max(1, len(current.actions) // 2)
        while chunk >= 1 and attempts < self._max_shrink_attempts and len(current.actions) > 1:
            changed = False
            start = 0
            while start < len(current.actions) and attempts < self._max_shrink_attempts:
                actions = current.actions[:start] + current.actions[start + chunk :]
                start += chunk
                if not actions:
                    continue
                candidate_case = current.model_copy(update={"actions": actions})
                attempts += 1
                replay = self._evaluate(
                    candidate_case, oracle, candidate=candidate, baseline=baseline
                )
                if replay.decision.verdict is Verdict.FAIL:
                    current = candidate_case
                    changed = True
                    break
            if not changed:
                chunk //= 2
        return current, attempts


def load_test_case(path: str | Path) -> TestCase:
    """Load and strictly validate a replay case."""
    return TestCase.model_validate_json(Path(path).read_text())
