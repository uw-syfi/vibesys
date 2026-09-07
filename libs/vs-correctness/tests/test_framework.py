from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vs_correctness import (
    ActionResult,
    CustomAction,
    Decision,
    Environment,
    HTTPAction,
    HTTPExecutor,
    Observation,
    Reference,
    Suite,
    TestCase,
    Verdict,
    Verifier,
    gate_exit_code,
    json_equal,
    write_report,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_correctness import GenerationContext, OracleContext


CANDIDATE = Environment(name="candidate", base_url="http://candidate", revision="c1")
BASELINE = Environment(name="baseline", base_url="http://baseline", revision="b1")


class FixedGenerator:
    def __init__(self, test_cases: list[TestCase]) -> None:
        self.test_cases = test_cases

    def generate(self, context: GenerationContext) -> list[TestCase]:
        assert context.random().random() == context.random().random()
        return self.test_cases[: context.cases]


class RecordingExecutor:
    def __init__(self, *, fail_on_length: int | None = None) -> None:
        self.calls: list[tuple[TestCase, Environment]] = []
        self.fail_on_length = fail_on_length

    def execute(self, test_case: TestCase, environment: Environment) -> Observation:
        self.calls.append((test_case, environment))
        error = "connection refused" if self.fail_on_length == len(test_case.actions) else None
        results = [
            ActionResult(phase=phase, index=index, status=200)
            for phase, actions in (
                ("setup", test_case.setup),
                ("actions", test_case.actions),
                ("cleanup", test_case.cleanup),
            )
            for index, _action in enumerate(actions)
        ]
        if error is not None:
            action_offset = len(test_case.setup)
            results[action_offset] = results[action_offset].model_copy(update={"error": error})
        return Observation(
            environment=environment,
            action_results=tuple(results),
        )


class MatchingOracle:
    def check(self, context: OracleContext) -> Decision:
        if context.baseline is None:
            return Decision(verdict=Verdict.PASS)
        return Decision(
            verdict=Verdict.PASS
            if context.baseline.action_results[0].status
            == context.candidate.action_results[0].status
            else Verdict.FAIL
        )


def make_case(size: int = 1) -> TestCase:
    return TestCase(
        id="case-1",
        setup=(HTTPAction(method="POST", path="/reset"),),
        actions=tuple(HTTPAction(method="GET", path=f"/items/{index}") for index in range(size)),
        cleanup=(HTTPAction(method="DELETE", path="/fixture"),),
    )


def test_differential_uses_identical_serialized_case_and_passes() -> None:
    executor = RecordingExecutor()
    report = Verifier(executor).verify(
        Suite(FixedGenerator([make_case()]), MatchingOracle()),
        candidate=CANDIDATE,
        baseline=BASELINE,
        seed=12,
        cases=1,
    )

    assert report.passed
    assert gate_exit_code(report) == 0
    assert executor.calls[0][0].model_dump_json() == executor.calls[1][0].model_dump_json()
    assert executor.calls[0][1] == BASELINE
    assert executor.calls[1][1] == CANDIDATE


@pytest.mark.parametrize("verdict", [Verdict.FAIL, Verdict.INCONCLUSIVE])
def test_nonpassing_verdict_cannot_pass_gate(verdict: Verdict) -> None:
    class Oracle:
        def check(self, context: OracleContext) -> Decision:
            return Decision(verdict=verdict, reason=context.test_case.id)

    report = Verifier(RecordingExecutor(), max_shrink_attempts=0).verify(
        Suite(FixedGenerator([make_case()]), Oracle()), candidate=CANDIDATE, cases=1
    )
    assert not report.passed
    assert gate_exit_code(report) == 1


def test_transport_error_and_oracle_exception_are_inconclusive() -> None:
    class RaisingOracle:
        def check(self, context: OracleContext) -> Decision:
            raise RuntimeError(context.test_case.id)

    transport = Verifier(RecordingExecutor(fail_on_length=1)).verify(
        Suite(FixedGenerator([make_case()]), MatchingOracle()), candidate=CANDIDATE, cases=1
    )
    exception = Verifier(RecordingExecutor()).verify(
        Suite(FixedGenerator([make_case()]), RaisingOracle()), candidate=CANDIDATE, cases=1
    )
    assert transport.results[0].decision.verdict is Verdict.INCONCLUSIVE
    assert exception.results[0].decision.verdict is Verdict.INCONCLUSIVE


def test_empty_suite_is_rejected() -> None:
    with pytest.raises(ValueError, match="no test cases"):
        Verifier(RecordingExecutor()).verify(
            Suite(FixedGenerator([]), MatchingOracle()), candidate=CANDIDATE, cases=1
        )


def test_case_rejects_oracle_expectations_in_generator_output() -> None:
    with pytest.raises(ValueError, match="metadata"):
        TestCase.model_validate(
            {
                "id": "input-only",
                "actions": [{"kind": "http", "method": "GET", "path": "/items"}],
                "metadata": {"expected_status": 200},
            }
        )


@pytest.mark.parametrize(
    "mutation", ["missing", "reordered", "duplicate", "out-of-range", "wrong-environment"]
)
def test_misaligned_executor_observation_is_inconclusive(mutation: str) -> None:
    class MisalignedExecutor:
        def execute(self, _test_case: TestCase, environment: Environment) -> Observation:
            results = (
                ActionResult(phase="actions", index=0, status=200),
                ActionResult(phase="actions", index=1, status=200),
            )
            if mutation == "missing":
                results = results[:1]
            elif mutation == "reordered":
                results = tuple(reversed(results))
            elif mutation == "duplicate":
                results = (results[0], results[0])
            elif mutation == "out-of-range":
                results = (results[0], results[1].model_copy(update={"index": 7}))
            observed_environment = BASELINE if mutation == "wrong-environment" else environment
            return Observation(environment=observed_environment, action_results=results)

    test_case = TestCase(
        id="aligned",
        actions=(
            HTTPAction(method="GET", path="/first"),
            HTTPAction(method="GET", path="/second"),
        ),
    )
    report = Verifier(MisalignedExecutor()).verify(
        Suite(FixedGenerator([test_case]), MatchingOracle()), candidate=CANDIDATE, cases=1
    )

    assert report.results[0].decision.verdict is Verdict.INCONCLUSIVE


def test_generator_consumption_is_bounded() -> None:
    class InfiniteGenerator:
        def generate(self, context: GenerationContext):  # noqa: ANN202, ARG002
            index = 0
            while True:
                yield TestCase(id=str(index), actions=(HTTPAction(method="GET", path="/item"),))
                index += 1

    with pytest.raises(ValueError, match="limit is 2"):
        Verifier(RecordingExecutor()).verify(
            Suite(InfiniteGenerator(), MatchingOracle()), candidate=CANDIDATE, cases=2
        )


def test_http_action_rejects_environment_bypass_url() -> None:
    with pytest.raises(ValueError, match="absolute-path relative"):
        HTTPAction(method="GET", path="http://other.example/items")


def test_shrinking_retains_setup_cleanup_and_only_reproducible_failures() -> None:
    resets: list[Environment] = []

    class SizeOracle:
        def check(self, context: OracleContext) -> Decision:
            return Decision(
                verdict=Verdict.FAIL if len(context.test_case.actions) >= 2 else Verdict.PASS
            )

    report = Verifier(RecordingExecutor(), reset=resets.append, max_shrink_attempts=20).verify(
        Suite(FixedGenerator([make_case(8)]), SizeOracle()), candidate=CANDIDATE, cases=1
    )
    minimized = report.results[0].minimized_case
    assert minimized is not None
    assert len(minimized.actions) == 2
    assert minimized.setup == make_case().setup
    assert minimized.cleanup == make_case().cleanup
    assert len(resets) == report.results[0].shrink_attempts + 1


def test_infrastructure_failure_during_shrink_is_not_retained() -> None:
    class AlwaysFailOracle:
        def check(self, context: OracleContext) -> Decision:  # noqa: ARG002
            return Decision(verdict=Verdict.FAIL)

    report = Verifier(RecordingExecutor(fail_on_length=2), max_shrink_attempts=10).verify(
        Suite(FixedGenerator([make_case(4)]), AlwaysFailOracle()), candidate=CANDIDATE, cases=1
    )
    assert len(report.results[0].minimized_case.actions) == 3


def test_shrinking_recomputes_outcome_from_reduced_executable_inputs() -> None:
    seen: list[tuple[str, ...]] = []

    class InputDerivedOracle:
        def check(self, context: OracleContext) -> Decision:
            paths = tuple(action.path for action in context.test_case.actions)
            seen.append(paths)
            return Decision(
                verdict=Verdict.FAIL if "/bad" in paths else Verdict.PASS,
                reason="derived from request paths",
            )

    original = TestCase(
        id="input-derived",
        actions=tuple(
            HTTPAction(method="GET", path=path) for path in ("/good", "/bad", "/also-good")
        ),
    )
    report = Verifier(RecordingExecutor(), max_shrink_attempts=10).verify(
        Suite(FixedGenerator([original]), InputDerivedOracle()), candidate=CANDIDATE, cases=1
    )

    minimized = report.results[0].minimized_case
    assert minimized is not None
    assert tuple(action.path for action in minimized.actions) == ("/bad",)
    assert seen[0] == ("/good", "/bad", "/also-good")
    assert seen[-1] == ("/bad",)


def test_json_normalization_and_atomic_report(tmp_path: Path) -> None:
    left = {"request_id": "a", "features": [{"id": 2}, {"id": 1}]}
    right = {"request_id": "b", "features": [{"id": 1}, {"id": 2}]}
    assert json_equal(
        left,
        right,
        ignore_keys=frozenset({"request_id"}),
        unordered_paths=frozenset({("features",)}),
    )
    report = Verifier(RecordingExecutor()).verify(
        Suite(FixedGenerator([make_case()]), MatchingOracle()), candidate=CANDIDATE, cases=1
    )
    destination = tmp_path / "nested" / "report.json"
    write_report(report, destination)
    payload = json.loads(destination.read_text())
    assert payload["schema_version"] == 2
    assert payload["candidate"]["revision"] == "c1"
    assert payload["results"][0]["test_case"]["id"] == "case-1"


def test_references_resolve_independently_in_each_environment() -> None:
    class Response:
        status = 200

        class Headers(dict[str, str]):
            def get_all(self, key: str) -> list[str]:
                return [self[key]]

        headers = Headers({"Content-Type": "application/json"})

        def __init__(self, body: object) -> None:
            self._body = json.dumps(body).encode()

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return self._body

    class Opener:
        def __init__(self, token: str) -> None:
            self.token = token

        def open(self, request: object, timeout: float | None = None) -> Response:  # noqa: ARG002
            url = request.full_url  # type: ignore[attr-defined]
            if url.endswith("/token"):
                return Response({"token": self.token})
            return Response({"matched": url.endswith(f"?token={self.token}")})

        def close(self) -> None:
            pass

    openers = iter((Opener("baseline-token"), Opener("candidate-token")))
    with patch("urllib.request.build_opener", side_effect=lambda *_: next(openers)):
        test_case = TestCase(
            id="references",
            setup=(HTTPAction(method="GET", path="/token"),),
            actions=(
                HTTPAction(
                    method="GET",
                    path="/use",
                    query={
                        "token": Reference(
                            phase="setup", index=0, source="json", json_path=("token",)
                        )
                    },
                ),
            ),
        )
        observations = [
            HTTPExecutor().execute(
                test_case,
                Environment(name=str(index), base_url=f"http://environment-{index}"),
            )
            for index in range(2)
        ]
        assert all(
            observation.action_results[1].json_body() == {"matched": True}
            for observation in observations
        )
        assert observations[0].action_results[0].body != observations[1].action_results[0].body


def test_cleanup_error_fails_closed() -> None:
    class CleanupErrorExecutor(RecordingExecutor):
        def execute(
            self,
            test_case: TestCase,  # noqa: ARG002
            environment: Environment,
        ) -> Observation:
            return Observation(
                environment=environment,
                action_results=(
                    ActionResult(phase="actions", index=0, status=200),
                    ActionResult(phase="cleanup", index=0, error="cleanup failed"),
                ),
            )

    report = Verifier(CleanupErrorExecutor()).verify(
        Suite(FixedGenerator([make_case()]), MatchingOracle()), candidate=CANDIDATE, cases=1
    )
    assert report.results[0].decision.verdict is Verdict.INCONCLUSIVE
    assert gate_exit_code(report) == 1


def test_custom_action_serializes_for_user_executor() -> None:
    test_case = TestCase(
        id="custom", actions=(CustomAction(name="publish", payload={"topic": "orders"}),)
    )
    replay = TestCase.model_validate_json(test_case.model_dump_json())
    assert isinstance(replay.actions[0], CustomAction)
    assert replay.actions[0].payload == {"topic": "orders"}


def test_whole_body_reference_survives_serialization() -> None:
    test_case = TestCase(
        id="body-reference",
        setup=(HTTPAction(method="GET", path="/token"),),
        actions=(
            HTTPAction(
                method="POST",
                path="/use",
                body=Reference(phase="setup", index=0, source="json", json_path=("token",)),
            ),
        ),
    )
    replay = TestCase.model_validate_json(test_case.model_dump_json())
    assert isinstance(replay.actions[0], HTTPAction)
    assert isinstance(replay.actions[0].body, Reference)
