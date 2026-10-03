"""Byte-exact snapshots of every domain role before relocating prompt assets.

The manifest stores SHA-256 of UTF-8 output without whitespace normalization.
Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review the prompt diff.
Strategy prompt suites separately snapshot the composed agent-facing output.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from vibesys.constants import DomainName
from vibesys.orchestration.domains.base import DOMAIN_ROLES, DomainRole
from vibesys.orchestration.domains.registry import resolve_domain
from vibesys.orchestration.domains.rendering import render_domain_section

_SNAPSHOT = Path(__file__).parent / "fixtures" / "prompt_sha256.json"
_CASES: dict[str, dict[str, object]] = {
    "bare": {"accuracy_command": "", "benchmark_command": "", "workspace_sources": ()},
    "commands": {
        "accuracy_command": "uv run python accuracy_checker/checker.py",
        "benchmark_command": "uv run python benchmark/benchmark.py",
        "workspace_sources": (),
    },
    "seeded": {
        "accuracy_command": "uv run python accuracy_checker/checker.py",
        "benchmark_command": "uv run python benchmark/benchmark.py",
        "workspace_sources": (
            {"dest": "engine", "name": "candidate-engine"},
            {"dest": "oracle", "name": "reference-engine"},
        ),
    },
}


@pytest.mark.parametrize("domain", DomainName)
@pytest.mark.parametrize("role", DOMAIN_ROLES)
@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("execution", ["local", "remote"])
def test_domain_role_prompt_bytes_are_stable(
    domain: DomainName, role: DomainRole, case: str, execution: str
) -> None:
    rendered = render_domain_section(
        resolve_domain(domain),
        role,
        modality=None,
        interface="service",
        reference_path="/workspace/reference/main.py",
        runtime_notes="Runtime notes.",
        profile_execution=execution,
        **_CASES[case],
    )
    key = f"{domain.value}/{role.value}/{case}/{execution}"
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        snapshots = json.loads(_SNAPSHOT.read_text()) if _SNAPSHOT.exists() else {}
        snapshots[key] = digest
        _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT.write_text(json.dumps(snapshots, indent=2, sort_keys=True) + "\n")
    snapshots = json.loads(_SNAPSHOT.read_text())
    assert digest == snapshots[key], f"Domain prompt bytes changed: {key}"
