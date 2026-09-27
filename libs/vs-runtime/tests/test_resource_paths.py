"""Contracts for checkout/package tree resolution and owned SDK paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    BundledResources,
    InputProjectError,
    relative_sdk_source,
    resolve_bundled_tree,
    resolve_packaged_tree,
    resolve_sdk_source,
)

if TYPE_CHECKING:
    from pathlib import Path


def _installable_package(root: Path, name: str = "vs-bench") -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text(f"[project]\nname = {name!r}\n")
    return package


def test_bundled_tree_prefers_checkout_then_packaged_tree(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout" / "resources"
    packaged = tmp_path / "site-packages" / "product" / "_resources"
    checkout.mkdir(parents=True)
    packaged.mkdir(parents=True)

    def package_files(_package: str) -> Path:
        return packaged.parent

    assert (
        resolve_bundled_tree(
            checkout,
            package="product",
            packaged_subdir="_resources",
            package_files=package_files,
        )
        == checkout
    )
    checkout.rmdir()
    assert (
        resolve_bundled_tree(
            checkout,
            package="product",
            packaged_subdir="_resources",
            package_files=package_files,
        )
        == packaged
    )


def test_bundled_resources_exposes_existing_named_directories(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout" / "resources"
    profiler = checkout / "profilers" / "nsys"
    profiler.mkdir(parents=True)
    resources = BundledResources(checkout, package="product")

    assert resources.root() == checkout
    assert resources.directory("profilers", "nsys") == profiler
    assert resources.directory("profilers", "missing") is None


def test_bundled_resources_falls_back_to_installed_package(tmp_path: Path) -> None:
    packaged = tmp_path / "site-packages" / "product" / "_resources"
    skills = packaged / "skills"
    skills.mkdir(parents=True)
    resources = BundledResources(
        tmp_path / "missing-checkout",
        package="product",
        package_files=lambda _package: packaged.parent,
    )

    assert resources.root() == packaged
    assert resources.directory("skills") == skills


@pytest.mark.parametrize("children", [(), ("",), ("..",), ("nested/path",)])
def test_bundled_resources_rejects_non_child_names(
    tmp_path: Path, children: tuple[str, ...]
) -> None:
    resources = BundledResources(tmp_path / "resources", package="product")

    with pytest.raises(ValueError, match="non-empty direct names"):
        resources.directory(*children)


def test_packaged_tree_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one non-empty child"):
        resolve_packaged_tree(
            package="product",
            packaged_subdir="../secrets",
            package_files=lambda _package: tmp_path,
        )


def test_resolve_sdk_source_prefers_installable_checkout_package(tmp_path: Path) -> None:
    checkout_root = tmp_path / "repo" / "sdk"
    checkout = _installable_package(checkout_root)
    packaged_root = tmp_path / "site-packages" / "product" / "_sdk"
    _installable_package(packaged_root)
    project = tmp_path / "repo" / "examples" / "model-serving" / "input"
    project.mkdir(parents=True)

    resolved = resolve_sdk_source(
        project,
        "../../../sdk/vs-bench",
        checkout_sdk_root=checkout_root,
        packaged_sdk_root=packaged_root,
    )

    assert resolved == checkout
    assert (
        relative_sdk_source(
            resolved,
            checkout_sdk_root=checkout_root,
            packaged_sdk_root=packaged_root,
        ).as_posix()
        == "vs-bench"
    )


def test_resolve_sdk_source_maps_checkout_path_to_packaged_sdk(tmp_path: Path) -> None:
    checkout_root = tmp_path / "no-checkout" / "sdk"
    packaged_root = tmp_path / "site-packages" / "product" / "_sdk"
    packaged = _installable_package(packaged_root)
    project = tmp_path / "no-checkout" / "examples" / "input"
    project.mkdir(parents=True)

    assert (
        resolve_sdk_source(
            project,
            "../../../sdk/vs-bench",
            checkout_sdk_root=checkout_root,
            packaged_sdk_root=packaged_root,
        )
        == packaged
    )


@pytest.mark.parametrize(
    "raw_path",
    ["/sdk/vs-bench", "../../../other/vs-bench", "../../../sdk/../secrets"],
)
def test_resolve_sdk_source_rejects_paths_outside_owned_roots(
    tmp_path: Path, raw_path: str
) -> None:
    project = tmp_path / "repo" / "examples" / "input"
    project.mkdir(parents=True)

    with pytest.raises(InputProjectError, match="outside sdk/"):
        resolve_sdk_source(
            project,
            raw_path,
            checkout_sdk_root=tmp_path / "repo" / "sdk",
            packaged_sdk_root=tmp_path / "package" / "_sdk",
        )


def test_resolve_sdk_source_rejects_incomplete_package(tmp_path: Path) -> None:
    project = tmp_path / "repo" / "examples" / "input"
    project.mkdir(parents=True)
    packaged_root = tmp_path / "package" / "_sdk"
    (packaged_root / "unknown").mkdir(parents=True)

    with pytest.raises(InputProjectError, match=r"no pyproject\.toml"):
        resolve_sdk_source(
            project,
            "../../sdk/unknown",
            checkout_sdk_root=tmp_path / "repo" / "sdk",
            packaged_sdk_root=packaged_root,
        )
