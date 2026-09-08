"""User-defined microservice fuzzing and correctness framework."""

from vs_correctness.compare import json_equal, normalize_json
from vs_correctness.core import (
    Executor,
    GenerationContext,
    Generator,
    Oracle,
    Suite,
    Verifier,
    load_test_case,
)
from vs_correctness.http import HTTPExecutor
from vs_correctness.models import (
    Action,
    ActionResult,
    CaseResult,
    CustomAction,
    Decision,
    Environment,
    HTTPAction,
    Observation,
    OracleContext,
    Reference,
    TestCase,
    Verdict,
    VerificationReport,
)
from vs_correctness.reporting import gate_exit_code, write_report

__all__ = [
    "Action",
    "ActionResult",
    "CaseResult",
    "CustomAction",
    "Decision",
    "Environment",
    "Executor",
    "GenerationContext",
    "Generator",
    "HTTPAction",
    "HTTPExecutor",
    "Observation",
    "Oracle",
    "OracleContext",
    "Reference",
    "Suite",
    "TestCase",
    "Verdict",
    "VerificationReport",
    "Verifier",
    "gate_exit_code",
    "json_equal",
    "load_test_case",
    "normalize_json",
    "write_report",
]
