"""``vibesys.prompts.contexts``: shared prompt-context marshalling.

This module sits low in the module graph (``vibesys.prompts`` must not
import ``vibesys.orchestration`` or ``vibesys.domains``) and its functions
take only primitive/pydantic-safe values, never a live ``RunContext``. These
tests call its public functions directly: ``display_path``'s workspace-
relative formatting (including the trailing slash for directories),
``domain_context`` building the uniform ``DomainSectionContext``, and the two
``*_focus_kwargs`` narrowing helpers over a loosely typed extras mapping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.evaluators.input_manifest import WorkspaceSource
from vibesys.prompts.contexts import (
    DomainSectionContext,
    display_path,
    domain_context,
    implementer_focus_kwargs,
    plan_focus_kwargs,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# display_path
# ---------------------------------------------------------------------------


def test_display_path_file_has_no_trailing_slash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    file_path = workspace / "progress" / "plans" / "round-0012.json"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("{}")

    assert display_path(file_path, workspace) == "progress/plans/round-0012.json"


def test_display_path_directory_has_trailing_slash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    directory = workspace / "progress"
    directory.mkdir(parents=True)

    assert display_path(directory, workspace) == "progress/"


def test_display_path_is_workspace_relative(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "a" / "b" / "c.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("x")

    assert display_path(nested, workspace) == "a/b/c.md"


# ---------------------------------------------------------------------------
# domain_context
# ---------------------------------------------------------------------------


def _source() -> WorkspaceSource:
    return WorkspaceSource(
        name="ref", repo="https://example.com/ref.git", commit="a" * 40, dest="ref"
    )


def test_domain_context_builds_frozen_model_with_all_fields() -> None:
    sources = (_source(),)

    context = domain_context(
        modality="text",
        interface="cli",
        reference_path="reference/",
        benchmark_command="./bench.sh",
        accuracy_command="./check.sh",
        runtime_notes="single GPU",
        profile_execution="nsys profile ./bench.sh",
        workspace_sources=sources,
    )

    assert isinstance(context, DomainSectionContext)
    assert context.modality == "text"
    assert context.interface == "cli"
    assert context.reference_path == "reference/"
    assert context.benchmark_command == "./bench.sh"
    assert context.accuracy_command == "./check.sh"
    assert context.runtime_notes == "single GPU"
    assert context.profile_execution == "nsys profile ./bench.sh"
    assert context.workspace_sources == sources


def test_domain_context_allows_none_for_optional_fields() -> None:
    context = domain_context(
        modality=None,
        interface="cli",
        reference_path="reference/",
        benchmark_command=None,
        accuracy_command=None,
        runtime_notes="",
        profile_execution="",
        workspace_sources=(),
    )

    assert context.modality is None
    assert context.benchmark_command is None
    assert context.accuracy_command is None
    assert context.workspace_sources == ()


def test_domain_section_context_is_frozen() -> None:
    context = domain_context(
        modality=None,
        interface="cli",
        reference_path="reference/",
        benchmark_command=None,
        accuracy_command=None,
        runtime_notes="",
        profile_execution="",
        workspace_sources=(),
    )

    with pytest.raises(ValidationError):
        context.interface = "changed"  # ty: ignore[invalid-assignment]


# ---------------------------------------------------------------------------
# plan_focus_kwargs / implementer_focus_kwargs
# ---------------------------------------------------------------------------


def test_plan_focus_kwargs_defaults_for_a_plain_strategy() -> None:
    kwargs = plan_focus_kwargs({})

    assert kwargs == {
        "active_component": None,
        "ledger_text": None,
        "ranked_bottlenecks": [],
    }


def test_plan_focus_kwargs_narrows_a_profile_guided_strategys_extras() -> None:
    extra = {
        "active_component": "attention-kernel",
        "ledger_text": "bottleneck: memory bandwidth",
        "ranked_bottlenecks": [{"name": "attention", "share": 0.4}],
        "unrelated_field": "ignored",
    }

    kwargs = plan_focus_kwargs(extra)

    assert kwargs["active_component"] == "attention-kernel"
    assert kwargs["ledger_text"] == "bottleneck: memory bandwidth"
    assert kwargs["ranked_bottlenecks"] == [{"name": "attention", "share": 0.4}]
    assert "unrelated_field" not in kwargs


def test_implementer_focus_kwargs_defaults_for_a_plain_strategy() -> None:
    assert implementer_focus_kwargs({}) == {"active_component": None}


def test_implementer_focus_kwargs_narrows_active_component() -> None:
    kwargs = implementer_focus_kwargs({"active_component": "decode-loop", "extra": 1})

    assert kwargs == {"active_component": "decode-loop"}
