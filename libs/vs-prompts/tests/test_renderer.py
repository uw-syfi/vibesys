from __future__ import annotations

from typing import TYPE_CHECKING

import jinja2
import pytest

from vs_prompts.renderer import TemplateRenderer

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_render_template_basic(tmp_path: Path) -> None:
    _write(tmp_path / "greeting.j2", "Hello {{ name }}!")
    renderer = TemplateRenderer(tmp_path)

    assert renderer.render_template("greeting.j2", name="World") == "Hello World!"


def test_render_string_matches_file_semantics(tmp_path: Path) -> None:
    renderer = TemplateRenderer(tmp_path)

    assert renderer.render_string("Hello {{ name }}!", name="World") == "Hello World!"


def test_missing_var_raises_undefined_error(tmp_path: Path) -> None:
    _write(tmp_path / "t.j2", "{{ missing }}")
    renderer = TemplateRenderer(tmp_path)

    with pytest.raises(jinja2.UndefinedError):
        renderer.render_template("t.j2")


def test_bare_if_on_missing_var_raises(tmp_path: Path) -> None:
    """Unlike Jinja's default Undefined, a bare truthiness check on a
    never-supplied variable is itself an error under StrictUndefined — this
    is the exact bug class the library exists to catch: a template silently
    treating an absent variable as falsy instead of failing loudly.
    """
    _write(tmp_path / "t.j2", "{% if runtime_notes %}{{ runtime_notes }}{% endif %}")
    renderer = TemplateRenderer(tmp_path)

    with pytest.raises(jinja2.UndefinedError):
        renderer.render_template("t.j2")


def test_is_defined_guard_survives_strict_undefined(tmp_path: Path) -> None:
    """``is defined`` never evaluates the value, so it must not raise even
    when the variable was never supplied — defensive guards written before
    strict-undefined was turned on must keep working unchanged.
    """
    _write(
        tmp_path / "t.j2",
        "{% if runtime_notes is defined and runtime_notes %}{{ runtime_notes }}{% endif %}done",
    )
    renderer = TemplateRenderer(tmp_path)

    assert renderer.render_template("t.j2") == "done"


def test_present_but_falsy_var_does_not_raise(tmp_path: Path) -> None:
    _write(tmp_path / "t.j2", "{% if runtime_notes %}{{ runtime_notes }}{% endif %}done")
    renderer = TemplateRenderer(tmp_path)

    assert renderer.render_template("t.j2", runtime_notes="") == "done"


def test_fallback_roots_resolve_shared_include(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared"
    loop_root = tmp_path / "loop"
    _write(shared_root / "_fragment.j2", "shared:{{ value }}")
    _write(loop_root / "page.j2", '{% include "_fragment.j2" %}')
    renderer = TemplateRenderer(loop_root, fallback_roots=(shared_root,))

    assert renderer.render_template("page.j2", value="x") == "shared:x"


def test_unused_kwargs_flags_a_key_the_template_never_references(tmp_path: Path) -> None:
    _write(tmp_path / "t.j2", "{{ objective }}")
    renderer = TemplateRenderer(tmp_path)

    assert renderer.unused_kwargs("t.j2", objective="obj", runtime_notes="dropped") == frozenset(
        {"runtime_notes"}
    )


def test_unused_kwargs_empty_when_every_kwarg_is_referenced(tmp_path: Path) -> None:
    _write(tmp_path / "t.j2", "{{ objective }}{{ runtime_notes }}")
    renderer = TemplateRenderer(tmp_path)

    assert renderer.unused_kwargs("t.j2", objective="obj", runtime_notes="notes") == frozenset()


def test_unused_kwargs_catches_a_key_only_used_in_an_untaken_branch(tmp_path: Path) -> None:
    """Static analysis, not execution: a var referenced only inside a branch
    this particular call doesn't take still counts as "used" — the check is
    about what the *template* reads across all its branches, not what one
    specific render happened to touch.
    """
    _write(tmp_path / "t.j2", "{% if flag %}{{ runtime_notes }}{% endif %}")
    renderer = TemplateRenderer(tmp_path)

    assert renderer.unused_kwargs("t.j2", flag=False, runtime_notes="notes") == frozenset()


def test_unused_kwargs_resolves_through_includes(tmp_path: Path) -> None:
    _write(tmp_path / "_fragment.j2", "{{ runtime_notes }}")
    _write(tmp_path / "t.j2", '{% include "_fragment.j2" %}')
    renderer = TemplateRenderer(tmp_path)

    assert renderer.unused_kwargs("t.j2", runtime_notes="notes", extra="dropped") == frozenset(
        {"extra"}
    )


def test_child_renderer_falls_back_to_parent_root(tmp_path: Path) -> None:
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "parent" / "child"
    _write(parent_root / "_shared.j2", "shared:{{ value }}")
    _write(child_root / "page.j2", '{% include "_shared.j2" %}')
    parent = TemplateRenderer(parent_root)
    child = parent.child(child_root)

    assert child.render_template("page.j2", value="y") == "shared:y"
