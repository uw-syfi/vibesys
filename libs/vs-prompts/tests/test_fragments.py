from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_prompts.fragments import FragmentFamily
from vs_prompts.renderer import TemplateRenderer

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_validate_passes_when_all_files_present(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    _write(root / "cuda" / "device_dtype.j2", "bfloat16")
    _write(root / "metal" / "device_dtype.j2", "")  # empty = deliberate skip
    family = FragmentFamily(root=root, names=frozenset({"device_dtype"}))

    family.validate(keys=["cuda", "metal"])


def test_validate_raises_listing_missing_files(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    _write(root / "cuda" / "device_dtype.j2", "bfloat16")
    # metal/device_dtype.j2 never created
    family = FragmentFamily(root=root, names=frozenset({"device_dtype"}))

    with pytest.raises(ValueError, match="metal"):
        family.validate(keys=["cuda", "metal"])


def test_render_all_returns_dict_keyed_by_name(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    _write(root / "cuda" / "device_dtype.j2", "bfloat16")
    _write(root / "cuda" / "profiling_workflow.j2", "nsys profile ...")
    renderer = TemplateRenderer(tmp_path)
    family = FragmentFamily(root=root, names=frozenset({"device_dtype", "profiling_workflow"}))

    rendered = family.render_all("cuda", renderer)

    assert rendered == {"device_dtype": "bfloat16", "profiling_workflow": "nsys profile ..."}


def test_render_unknown_name_raises(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    _write(root / "cuda" / "device_dtype.j2", "bfloat16")
    renderer = TemplateRenderer(tmp_path)
    family = FragmentFamily(root=root, names=frozenset({"device_dtype"}))

    with pytest.raises(ValueError, match="Unknown fragment"):
        family.render("cuda", "not_a_real_fragment", renderer)


def test_render_strips_trailing_newline(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    _write(root / "cuda" / "device_dtype.j2", "bfloat16\n")
    renderer = TemplateRenderer(tmp_path)
    family = FragmentFamily(root=root, names=frozenset({"device_dtype"}))

    assert family.render("cuda", "device_dtype", renderer) == "bfloat16"


def test_render_all_resolves_via_fallback_root(tmp_path: Path) -> None:
    """The fragment family's root lives under a *fallback* root, not the
    renderer's own root — the real vibesys topology, where a per-loop
    renderer (root=``prompts/loops/multi``) still needs to reach shared
    ``prompts/backend/`` fragments via its fallback to ``prompts/``.
    """
    shared_root = tmp_path / "shared"
    loop_root = tmp_path / "shared" / "loops" / "multi"
    _write(shared_root / "backend" / "cuda" / "device_dtype.j2", "bfloat16")
    loop_root.mkdir(parents=True, exist_ok=True)
    renderer = TemplateRenderer(loop_root, fallback_roots=(shared_root,))
    family = FragmentFamily(root=shared_root / "backend", names=frozenset({"device_dtype"}))

    assert family.render_all("cuda", renderer) == {"device_dtype": "bfloat16"}


def test_render_raises_when_root_unreachable_from_renderer(tmp_path: Path) -> None:
    unrelated_root = tmp_path / "unrelated"
    _write(unrelated_root / "backend" / "cuda" / "device_dtype.j2", "bfloat16")
    renderer = TemplateRenderer(tmp_path / "elsewhere")
    family = FragmentFamily(root=unrelated_root / "backend", names=frozenset({"device_dtype"}))

    with pytest.raises(ValueError, match="is not"):
        family.render("cuda", "device_dtype", renderer)


def test_auto_injected_fragment_is_usable_as_inline_variable(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    _write(root / "cuda" / "device_dtype.j2", "bfloat16")
    _write(tmp_path / "parent.j2", "Use {{ device_dtype }} tensors.")
    renderer = TemplateRenderer(tmp_path)
    family = FragmentFamily(root=root, names=frozenset({"device_dtype"}))

    rendered = renderer.render_template("parent.j2", **family.render_all("cuda", renderer))

    assert rendered == "Use bfloat16 tensors."
