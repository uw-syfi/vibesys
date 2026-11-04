"""A run's domain, not the runtime library, says what its server prints when a candidate breaks it."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from tests.support.task_project import write_task

from vibesys.constants import DomainName
from vibesys.domains.registry import DOMAINS, resolve_domain
from vibesys.inputs import load_task
from vibesys.run.evaluation import trusted_evaluation_plan
from vs_runtime.api import BenchmarkFailureKind
from vs_runtime.api.infrastructure import RecordState, TerminalSignal, classify, signal_of

if TYPE_CHECKING:
    from pathlib import Path

# The startup-failure causes the runtime classified as the candidate's before they moved to
# the domain that owns them, each with a log line that matches it.
_LLM_SERVING_LOGS = (
    "ValueError: No available memory for the cache blocks",
    "ValueError: The decoder prompt (length 9000) is larger than the maximum number of "
    "tokens that can be stored in KV cache",
)


def _kinds(log: str, causes: tuple[str, ...]) -> set[BenchmarkFailureKind]:
    signal = signal_of(
        None, service_not_ready=True, service_log=log, candidate_startup_causes=causes
    )
    return {classify(signal, record) for record in RecordState}


def test_every_domain_declares_valid_patterns() -> None:
    for definition in DOMAINS.values():
        for pattern in definition.candidate_startup_causes:
            re.compile(pattern)


def test_llm_serving_classifies_its_servers_failures_as_it_always_did() -> None:
    causes = DOMAINS[DomainName.LLM_SERVING].candidate_startup_causes
    for log in _LLM_SERVING_LOGS:
        assert _kinds(log, causes) == {BenchmarkFailureKind.WORKLOAD}


@pytest.mark.parametrize("name", [n for n in DomainName if n is not DomainName.LLM_SERVING])
def test_a_domain_without_server_causes_gets_only_the_generic_facts(name: DomainName) -> None:
    causes = DOMAINS[name].candidate_startup_causes
    assert causes == ()
    for log in _LLM_SERVING_LOGS:
        assert _kinds(log, causes) == {BenchmarkFailureKind.AMBIGUOUS}
    for log in ("CUDA out of memory", "error: unrecognized arguments: --x"):
        assert _kinds(log, causes) == {BenchmarkFailureKind.WORKLOAD}
    assert TerminalSignal.NO_EXIT_STATUS is signal_of(None)


@pytest.mark.parametrize("name", list(DomainName))
def test_the_plan_carries_the_causes_the_wiring_passes(name: DomainName, tmp_path: Path) -> None:
    write_task(
        tmp_path,
        f'version = 1\n[agent]\ndomain = "{name.value}"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
    )

    plan = trusted_evaluation_plan(
        load_task(tmp_path),
        accuracy_command=None,
        benchmark_command=None,
        framework_setup_timeout_seconds=0,
        candidate_startup_causes=resolve_domain(name).candidate_startup_causes,
    )

    assert plan.candidate_startup_causes == DOMAINS[name].candidate_startup_causes
