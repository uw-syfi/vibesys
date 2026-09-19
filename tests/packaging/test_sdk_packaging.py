"""Contracts for locating and resolving the bundled input SDK."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import pytest

from vibesys import sdk_paths
from vibesys.input_project import InputProjectError


def _installable_package(root: Path, name: str = "vs-bench") -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text(f"[project]\nname = {name!r}\n")
    return package


def test_sdk_root_prefers_the_checkout(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    checkout = tmp_path / "checkout"
    packaged = tmp_path / "site-packages" / "vibesys" / "_sdk"
    (checkout / "sdk").mkdir(parents=True)
    packaged.mkdir(parents=True)

    monkeypatch.setattr(sdk_paths, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(sdk_paths, "files", lambda _package: packaged.parent)

    assert sdk_paths.sdk_root() == checkout / "sdk"


def test_sdk_root_falls_back_to_the_packaged_copy(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    packaged = tmp_path / "site-packages" / "vibesys" / "_sdk"
    packaged.mkdir(parents=True)

    monkeypatch.setattr(sdk_paths, "PROJECT_ROOT", tmp_path / "no-checkout")
    monkeypatch.setattr(sdk_paths, "files", lambda _package: packaged.parent)

    assert sdk_paths.sdk_root() == packaged


def test_resolve_sdk_source_prefers_an_installable_checkout_package(tmp_path):  # noqa: ANN001, ANN201
    checkout_root = tmp_path / "repo" / "sdk"
    checkout = _installable_package(checkout_root)
    packaged_root = tmp_path / "site-packages" / "vibesys" / "_sdk"
    _installable_package(packaged_root)
    project = tmp_path / "repo" / "examples" / "model-serving" / "input"
    project.mkdir(parents=True)

    resolved = sdk_paths.resolve_sdk_source(
        project,
        "../../../sdk/vs-bench",
        checkout_sdk_root=checkout_root,
        packaged_sdk_root=packaged_root,
    )

    assert resolved == checkout


def test_resolve_sdk_source_maps_a_repo_relative_path_to_packaged_sdk(tmp_path):  # noqa: ANN001, ANN201
    checkout_root = tmp_path / "no-checkout" / "sdk"
    packaged_root = tmp_path / "site-packages" / "vibesys" / "_sdk"
    packaged = _installable_package(packaged_root)
    project = tmp_path / "no-checkout" / "examples" / "model-serving" / "input"
    project.mkdir(parents=True)

    resolved = sdk_paths.resolve_sdk_source(
        project,
        "../../../sdk/vs-bench",
        checkout_sdk_root=checkout_root,
        packaged_sdk_root=packaged_root,
    )

    assert resolved == packaged


@pytest.mark.parametrize(
    "raw_path",
    ["/sdk/vs-bench", "../../../other/vs-bench", "../../../sdk/../secrets"],
)
def test_resolve_sdk_source_rejects_paths_outside_the_sdk(tmp_path, raw_path):  # noqa: ANN001, ANN201
    project = tmp_path / "repo" / "examples" / "input"
    project.mkdir(parents=True)

    with pytest.raises(InputProjectError, match="outside sdk/"):
        sdk_paths.resolve_sdk_source(
            project,
            raw_path,
            checkout_sdk_root=tmp_path / "repo" / "sdk",
            packaged_sdk_root=tmp_path / "package" / "_sdk",
        )


def test_resolve_sdk_source_rejects_unknown_or_incomplete_packages(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "repo" / "examples" / "input"
    project.mkdir(parents=True)
    packaged_root = tmp_path / "package" / "_sdk"
    (packaged_root / "unknown").mkdir(parents=True)

    with pytest.raises(InputProjectError, match=r"no pyproject\.toml"):
        sdk_paths.resolve_sdk_source(
            project,
            "../../sdk/unknown",
            checkout_sdk_root=tmp_path / "repo" / "sdk",
            packaged_sdk_root=packaged_root,
        )


def test_resolve_sdk_source_rejects_an_installable_project_outside_owned_roots(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "external" / "input"
    project.mkdir(parents=True)
    _installable_package(tmp_path / "external" / "sdk", "evil")

    with pytest.raises(InputProjectError, match="outside sdk/"):
        sdk_paths.resolve_sdk_source(
            project,
            "../sdk/evil",
            checkout_sdk_root=tmp_path / "repo" / "sdk",
            packaged_sdk_root=None,
        )
