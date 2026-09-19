"""Snapshot tests for environment-specific runtime prompt templates.

``src/vibesys/prompts/environments/<kind>/*.j2`` used to be Python
string-builder functions embedded in ``run_environment.py`` (issue #378):
prompt-wording changes touched an infrastructure module and had no
diffable-fixture coverage, unlike every other prompt in the repo. These
snapshots give this content the same "wording change shows up as a
reviewable diff" discipline ``tests/vibesys/loops/*/fixtures/prompt_snapshots``
already gives domain/modality prompts.

Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest
tests/vibesys/sandbox/test_environment_prompt_snapshots.py``. Review the fixture
diff as the prompt diff -- do not blindly accept a regenerated snapshot.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

import pytest

from vibesys.evaluators.input_manifest import WorkspaceSource
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.prompts.renderer import _build_env

_ENVIRONMENTS_DIR = PROMPTS_DIR / "environments"
_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "environment_prompt_snapshots"

_WORKSPACE_SOURCE_A = WorkspaceSource(
    name="reference",
    repo="https://example.invalid/ref.git",
    commit="1234567abcdef0",
    dest="reference",
)
_WORKSPACE_SOURCE_B = WorkspaceSource(
    name="draft", repo="https://example.invalid/draft.git", commit="fedcba7654321", dest="draft"
)


def _snapshot_path(kind: str, case_name: str, template_name: str) -> Path:
    return _SNAPSHOT_DIR / kind / case_name / f"{template_name}.md"


def _assert_matches_snapshot(kind: str, case_name: str, template_name: str, rendered: str) -> None:
    snapshot = _snapshot_path(kind, case_name, template_name)
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(rendered)
        return
    expected = snapshot.read_text()
    if rendered == expected:
        return
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(snapshot),
            tofile=str(Path("rendered") / kind / case_name / f"{template_name}.md"),
        )
    )
    pytest.fail(f"Rendered environment prompt changed: {snapshot}\n{diff}")


_MODAL_RUNTIME_NOTES_CASES = {
    "cold_start": {
        "gpu": "H100",
        "app_name": "run-9f2a3b",
        "workspace_sources": (),
        "reference_path": "reference",
        "history_root": None,
    },
    "with_seeded_checkouts_and_history": {
        "gpu": "A100-80GB",
        "app_name": "run-c71de0",
        "workspace_sources": (_WORKSPACE_SOURCE_A, _WORKSPACE_SOURCE_B),
        "reference_path": "reference",
        "history_root": Path("/opt/vibesys-history"),
    },
}


@pytest.mark.parametrize("case_name,context", _MODAL_RUNTIME_NOTES_CASES.items())  # noqa: PT006  # tracked: #288
def test_modal_runtime_notes_snapshot(case_name: str, context: dict[str, object]) -> None:
    rendered = render_template("modal/runtime_notes.j2", template_dir=_ENVIRONMENTS_DIR, **context)
    _assert_matches_snapshot("modal", case_name, "runtime_notes", rendered)


def test_modal_prompt_notes_snapshot() -> None:
    rendered = render_template(
        "modal/prompt_notes.j2",
        template_dir=_ENVIRONMENTS_DIR,
        runtime_container_path="/opt/vibesys-runtime/environment.md",
    )
    _assert_matches_snapshot("modal", "pointer", "prompt_notes", rendered)


def test_modal_candidate_override_snapshot() -> None:
    base_prompt_notes = render_template(
        "modal/prompt_notes.j2",
        template_dir=_ENVIRONMENTS_DIR,
        runtime_container_path="/opt/vibesys-runtime/environment.md",
    )
    rendered = render_template(
        "modal/candidate_override.j2",
        template_dir=_ENVIRONMENTS_DIR,
        prompt_notes=base_prompt_notes,
        base_name="run-9f2a3b",
        candidate_name="run-9f2a3b-g2c5",
    )
    _assert_matches_snapshot("modal", "candidate_override", "candidate_override", rendered)


_DOCKER_PROMPT_NOTES_CASES = {
    "no_history": {"history_root": None},
    "with_history": {"history_root": Path("/opt/vibesys-history")},
}


@pytest.mark.parametrize("case_name,context", _DOCKER_PROMPT_NOTES_CASES.items())  # noqa: PT006  # tracked: #288
def test_docker_prompt_notes_snapshot(case_name: str, context: dict[str, object]) -> None:
    rendered = render_template("docker/prompt_notes.j2", template_dir=_ENVIRONMENTS_DIR, **context)
    _assert_matches_snapshot("docker", case_name, "prompt_notes", rendered)


def test_environment_templates_use_every_kwarg_their_call_site_passes() -> None:
    """Over-supply check: each real run_environment.py call site's exact
    kwargs, none silently unused by the template that renders them.
    """
    renderer = _build_env(_ENVIRONMENTS_DIR)
    cases = [
        (
            "modal/runtime_notes.j2",
            {
                "gpu": "H100",
                "app_name": "run-9f2a3b",
                "workspace_sources": (_WORKSPACE_SOURCE_A,),
                "reference_path": "reference",
                "history_root": Path("/opt/vibesys-history"),
            },
        ),
        (
            "modal/prompt_notes.j2",
            {"runtime_container_path": "/opt/vibesys-runtime/environment.md"},
        ),
        (
            "modal/candidate_override.j2",
            {
                "prompt_notes": "Runtime instructions are at `x`.",
                "base_name": "run-9f2a3b",
                "candidate_name": "run-9f2a3b-g2c5",
            },
        ),
        ("docker/prompt_notes.j2", {"history_root": Path("/opt/vibesys-history")}),
    ]
    failures = []
    for name, kwargs in cases:
        unused = renderer.unused_kwargs(name, **kwargs)
        if unused:
            failures.append(f"{name}: silently drops {sorted(unused)}")
    assert not failures, "\n".join(failures)
