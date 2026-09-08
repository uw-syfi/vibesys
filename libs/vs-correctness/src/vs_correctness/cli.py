"""Command-line gate for Python-defined correctness suites."""

from __future__ import annotations

import argparse
import importlib
from typing import cast

from vs_correctness.core import Suite, Verifier, load_test_case
from vs_correctness.http import HTTPExecutor
from vs_correctness.models import Environment, VerificationReport
from vs_correctness.reporting import gate_exit_code, write_report


def main(argv: list[str] | None = None) -> int:
    """Run a suite or replay case and return the fail-closed gate status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, help="importable module:attribute Suite")
    parser.add_argument("--candidate-url", required=True)
    parser.add_argument("--candidate-revision")
    parser.add_argument("--baseline-url")
    parser.add_argument("--baseline-revision")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--cases", type=int, default=100)
    parser.add_argument("--max-shrink-attempts", type=int, default=32)
    parser.add_argument("--replay", help="serialized TestCase JSON to replay")
    parser.add_argument("--report", required=True)
    arguments = parser.parse_args(argv)

    suite = _load_suite(arguments.suite)
    candidate = Environment(
        name="candidate",
        base_url=arguments.candidate_url,
        revision=arguments.candidate_revision,
    )
    baseline = (
        Environment(
            name="baseline",
            base_url=arguments.baseline_url,
            revision=arguments.baseline_revision,
        )
        if arguments.baseline_url
        else None
    )
    verifier = Verifier(HTTPExecutor(), max_shrink_attempts=arguments.max_shrink_attempts)
    if arguments.replay:
        result = verifier.replay(
            load_test_case(arguments.replay),
            suite.oracle,
            candidate=candidate,
            baseline=baseline,
        )
        report = VerificationReport(
            seed=arguments.seed,
            candidate=candidate,
            baseline=baseline,
            results=(result,),
        )
    else:
        report = verifier.verify(
            suite,
            candidate=candidate,
            baseline=baseline,
            seed=arguments.seed,
            cases=arguments.cases,
        )
    write_report(report, arguments.report)
    return gate_exit_code(report)


def _load_suite(reference: str) -> Suite:
    """Resolve a Suite instance or zero-argument Suite factory."""
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("--suite must have the form module:attribute")  # noqa: TRY003
    value = getattr(importlib.import_module(module_name), attribute_name)
    if callable(value):
        value = value()
    if not isinstance(value, Suite):
        raise TypeError(f"{reference} did not resolve to a Suite")  # noqa: TRY003
    return cast("Suite", value)
