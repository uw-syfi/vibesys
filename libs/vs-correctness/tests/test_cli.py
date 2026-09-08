from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from vs_correctness import (
    ActionResult,
    Decision,
    Environment,
    GenerationContext,
    HTTPAction,
    Observation,
    OracleContext,
    Suite,
    Verdict,
    cli,
)
from vs_correctness import (
    TestCase as CorrectnessCase,
)

if TYPE_CHECKING:
    from pathlib import Path


class OneCaseGenerator:
    def generate(self, context: GenerationContext) -> list[CorrectnessCase]:
        return [
            CorrectnessCase(
                id=f"generated-{context.seed}",
                actions=(HTTPAction(method="GET", path="/health"),),
            )
        ]


class FixedOracle:
    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict

    def check(self, context: OracleContext) -> Decision:
        del context
        return Decision(verdict=self.verdict, reason=f"oracle returned {self.verdict}")


def _suite(verdict: Verdict) -> Suite:
    return Suite(generator=OneCaseGenerator(), oracle=FixedOracle(verdict))


def _successful_observation(test_case: CorrectnessCase, environment: Environment) -> Observation:
    return Observation(
        environment=environment,
        action_results=tuple(
            ActionResult(phase="actions", index=index, status=200, body='{"ok":true}')
            for index, _action in enumerate(test_case.actions)
        ),
    )


@pytest.mark.parametrize(
    ("verdict", "expected_exit"),
    [(Verdict.PASS, 0), (Verdict.FAIL, 1), (Verdict.INCONCLUSIVE, 1)],
)
def test_main_writes_report_and_fails_closed_for_oracle_verdicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: Verdict,
    expected_exit: int,
) -> None:
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(cli, "_load_suite", lambda _reference: _suite(verdict))
    monkeypatch.setattr(
        cli.HTTPExecutor,
        "execute",
        lambda _self, case, env: _successful_observation(case, env),
    )

    exit_code = cli.main(
        [
            "--suite",
            "fixtures:suite",
            "--candidate-url",
            "http://candidate.test",
            "--candidate-revision",
            "candidate-sha",
            "--seed",
            "17",
            "--cases",
            "1",
            "--report",
            str(report_path),
        ]
    )

    report = json.loads(report_path.read_text())
    assert exit_code == expected_exit
    assert report["schema_version"] == 2
    assert report["seed"] == 17
    assert report["candidate"]["revision"] == "candidate-sha"
    assert report["results"][0]["test_case"]["id"] == "generated-17"
    assert report["results"][0]["decision"]["verdict"] == verdict


def test_main_replays_serialized_case_against_candidate_and_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay_path = tmp_path / "case.json"
    report_path = tmp_path / "report.json"
    replay_path.write_text(
        CorrectnessCase(
            id="saved-case",
            actions=(HTTPAction(method="GET", path="/saved"),),
        ).model_dump_json()
    )
    monkeypatch.setattr(cli, "_load_suite", lambda _reference: _suite(Verdict.PASS))
    seen: list[Environment] = []

    def execute(_self: object, case: CorrectnessCase, environment: Environment) -> Observation:
        seen.append(environment)
        return _successful_observation(case, environment)

    monkeypatch.setattr(cli.HTTPExecutor, "execute", execute)

    exit_code = cli.main(
        [
            "--suite",
            "fixtures:suite",
            "--candidate-url",
            "http://candidate.test",
            "--baseline-url",
            "http://baseline.test",
            "--baseline-revision",
            "baseline-sha",
            "--replay",
            str(replay_path),
            "--report",
            str(report_path),
        ]
    )

    report = json.loads(report_path.read_text())
    assert exit_code == 0
    assert [environment.name for environment in seen] == ["baseline", "candidate"]
    assert report["results"][0]["test_case"]["id"] == "saved-case"
    assert report["baseline"]["revision"] == "baseline-sha"


@pytest.mark.parametrize(
    ("reference", "attribute", "error"),
    [
        ("invalid", None, ValueError),
        ("fixture_module:missing", None, AttributeError),
        ("fixture_module:not_suite", object(), TypeError),
    ],
)
def test_load_suite_rejects_invalid_references(
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    attribute: object | None,
    error: type[Exception],
) -> None:
    module = ModuleType("fixture_module")
    if attribute is not None:
        module.__dict__["not_suite"] = attribute
    monkeypatch.setitem(sys.modules, "fixture_module", module)

    with pytest.raises(error):
        cli._load_suite(reference)  # noqa: SLF001


def test_load_suite_calls_zero_argument_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _suite(Verdict.PASS)
    module = ModuleType("fixture_factory")
    module.__dict__["build_suite"] = lambda: expected
    monkeypatch.setitem(sys.modules, "fixture_factory", module)

    assert cli._load_suite("fixture_factory:build_suite") is expected  # noqa: SLF001
