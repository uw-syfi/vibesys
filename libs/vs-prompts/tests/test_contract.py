from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_prompts.contract import (
    ContractViolation,
    TemplateContract,
    filter_skip_marked,
    resolve_free_variables,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_resolve_free_variables_simple(tmp_path: Path) -> None:
    t = _write(tmp_path / "t.j2", "Hello {{ name }}, you are {{ age }}.")

    free, unresolved = resolve_free_variables(t, search_roots=(tmp_path,))

    assert free == frozenset({"name", "age"})
    assert unresolved == ()


def test_resolve_free_variables_recurses_through_static_include(tmp_path: Path) -> None:
    _write(tmp_path / "_fragment.j2", "{% if runtime_notes %}{{ runtime_notes }}{% endif %}")
    parent = _write(tmp_path / "parent.j2", 'Task: {{ task }}\n{% include "_fragment.j2" %}')

    free, unresolved = resolve_free_variables(parent, search_roots=(tmp_path,))

    assert free == frozenset({"task", "runtime_notes"})
    assert unresolved == ()


def test_resolve_free_variables_reports_unresolved_dynamic_include(tmp_path: Path) -> None:
    parent = _write(
        tmp_path / "parent.j2",
        '{% include "_modality/" ~ modality ~ "/profiler.j2" %}',
    )

    free, unresolved = resolve_free_variables(parent, search_roots=(tmp_path,))

    assert free == frozenset({"modality"})
    assert len(unresolved) == 1
    assert unresolved[0].template_path == parent


def test_resolve_free_variables_handles_include_cycles(tmp_path: Path) -> None:
    _write(tmp_path / "a.j2", '{{ from_a }}{% include "b.j2" %}')
    b = _write(tmp_path / "b.j2", '{{ from_b }}{% include "a.j2" %}')

    free, unresolved = resolve_free_variables(b, search_roots=(tmp_path,))

    assert free == frozenset({"from_a", "from_b"})
    assert unresolved == ()


def test_resolve_free_variables_missing_include_target_is_unresolved(tmp_path: Path) -> None:
    parent = _write(tmp_path / "parent.j2", '{% include "does_not_exist.j2" %}')

    free, unresolved = resolve_free_variables(parent, search_roots=(tmp_path,))

    assert free == frozenset()
    assert len(unresolved) == 1
    assert unresolved[0].source == "does_not_exist.j2"


def test_template_contract_flags_missing_required_var(tmp_path: Path) -> None:
    good = _write(tmp_path / "torch.j2", "{{ runtime_notes }}{{ objective }}")
    bad = _write(tmp_path / "nsys.j2", "{{ objective }}")
    contract = TemplateContract(required=frozenset({"runtime_notes", "objective"}))

    violations = contract.check([good, bad], search_roots=(tmp_path,))

    assert len(violations) == 1
    assert violations[0].path == bad
    assert violations[0].missing_vars == frozenset({"runtime_notes"})


def test_template_contract_honors_skip_marker(tmp_path: Path) -> None:
    macos = _write(
        tmp_path / "macos_cpu.j2",
        "{# vs-prompts:unused: runtime_notes - macOS-CPU profiling has no environment-specific contract #}\n"
        "{{ objective }}",
    )
    contract = TemplateContract(required=frozenset({"runtime_notes", "objective"}))

    violations = contract.check([macos], search_roots=(tmp_path,))

    assert violations == []


def test_template_contract_passes_when_all_present(tmp_path: Path) -> None:
    t = _write(tmp_path / "t.j2", "{{ runtime_notes }}{{ objective }}")
    contract = TemplateContract(required=frozenset({"runtime_notes", "objective"}))

    assert contract.check([t], search_roots=(tmp_path,)) == []


def test_resolve_free_variables_raises_on_syntax_error(tmp_path: Path) -> None:
    bad = _write(tmp_path / "t.j2", "{% if unclosed %}")

    with pytest.raises(ValueError, match="Cannot parse template"):
        resolve_free_variables(bad, search_roots=(tmp_path,))


def test_contract_violation_str_names_path_and_missing_vars(tmp_path: Path) -> None:
    violation = ContractViolation(
        path=tmp_path / "nsys.j2", missing_vars=frozenset({"runtime_notes"})
    )

    assert str(violation) == f"{tmp_path / 'nsys.j2'}: silently drops ['runtime_notes']"


def test_filter_skip_marked_removes_marked_names(tmp_path: Path) -> None:
    t = _write(
        tmp_path / "t.j2",
        "{# vs-prompts:unused: profile_execution - torch-only concept #}\n{{ objective }}",
    )

    assert filter_skip_marked(t, {"profile_execution", "objective"}) == frozenset({"objective"})


def test_filter_skip_marked_keeps_unmarked_names(tmp_path: Path) -> None:
    t = _write(tmp_path / "t.j2", "{{ objective }}")

    assert filter_skip_marked(t, {"profile_execution"}) == frozenset({"profile_execution"})


def test_filter_skip_marked_respects_custom_marker(tmp_path: Path) -> None:
    t = _write(tmp_path / "t.j2", "{# custom-skip: profile_execution - reason #}")

    assert filter_skip_marked(t, {"profile_execution"}, skip_marker="custom-skip") == frozenset()
