"""Contracts for validating and staging a prebuilt TUI payload."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path  # noqa: TC003

import pytest
from tui_packaging import (
    TuiPackagingError,
    stage_prebuilt_tui,
    validate_tui_payload,
)


def _write_payload(
    root: Path,
    *,
    target: str = "linux-x86_64",
    tui_version: str = "0.1.0",
    manifest_version: str | None = None,
) -> Path:
    files = {
        "bin/bun": b"#!/bin/sh\n",
        "app/dist/launcher.js": b"// launcher\n",
        "app/dist/self-test.js": b"// self-test\n",
        "app/package.json": json.dumps({"name": "@vibesys/tui", "version": tui_version}).encode(),
        "app/node_modules/@vibesys/backend-client/package.json": b'{"name":"@vibesys/backend-client"}\n',
        "app/node_modules/@vibesys/backend-client/dist/index.js": b"// backend client\n",
        "app/node_modules/@vibesys/core-state/package.json": b'{"name":"@vibesys/core-state"}\n',
        "app/node_modules/@vibesys/core-state/dist/index.js": b"// core state\n",
        "app/node_modules/@opentui/core/index.js": b"// core\n",
        "app/node_modules/@opentui/core-linux-x64/index.js": b"// native\n",
        "licenses/BUN-LICENSE.md": b"Bun license\n",
        "licenses/opentui-core.txt": b"OpenTUI license\n",
    }
    hashes: dict[str, str] = {}
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        hashes[relative] = hashlib.sha256(content).hexdigest()
    (root / "bin" / "bun").chmod(0o755)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": target,
                "bun_version": "1.3.9",
                "tui_version": manifest_version or tui_version,
                "files": hashes,
            }
        )
    )
    return root


def test_stage_prebuilt_tui_validates_and_copies_the_payload(tmp_path):  # noqa: ANN001, ANN201
    source = _write_payload(tmp_path / "source")
    destination = tmp_path / "destination"

    assert stage_prebuilt_tui(
        source,
        destination,
        required=True,
        expected_target="linux-x86_64",
    )
    assert (destination / "bin" / "bun").stat().st_mode & 0o111
    assert (destination / "app" / "dist" / "launcher.js").is_file()
    assert stage_prebuilt_tui(
        source,
        destination,
        required=True,
        expected_target="linux-x86_64",
    )


@pytest.mark.parametrize("version", ["1.4.0", "0.2.0-rc.1"])
def test_tui_payload_version_is_dynamic_and_uses_canonical_npm_semver(
    tmp_path: Path,
    version: str,
) -> None:
    source = _write_payload(tmp_path / "source", tui_version=version)

    validate_tui_payload(source, expected_target="linux-x86_64")


def test_tui_payload_rejects_manifest_and_package_version_drift(tmp_path: Path) -> None:
    source = _write_payload(
        tmp_path / "source",
        tui_version="0.2.0-rc.1",
        manifest_version="0.2.0-rc.2",
    )

    with pytest.raises(TuiPackagingError, match=r"package\.json"):
        validate_tui_payload(source, expected_target="linux-x86_64")


def test_tui_payload_rejects_checkout_specific_dependencies(tmp_path: Path) -> None:
    source = _write_payload(tmp_path / "source")
    package_path = source / "app" / "package.json"
    package = json.loads(package_path.read_text())
    package["dependencies"] = {
        "@vibesys/core-state": "@vibesys/core-state@file:///build/clients/core-state"
    }
    package_path.write_text(json.dumps(package))

    with pytest.raises(TuiPackagingError, match="local path"):
        validate_tui_payload(source, expected_target="linux-x86_64")


def test_optional_staging_without_a_payload_is_a_noop(tmp_path):  # noqa: ANN001, ANN201
    destination = tmp_path / "destination"

    assert not stage_prebuilt_tui(None, destination, required=False)
    assert not destination.exists()


def test_required_staging_rejects_a_missing_payload(tmp_path):  # noqa: ANN001, ANN201
    with pytest.raises(TuiPackagingError, match="TUI payload"):
        stage_prebuilt_tui(tmp_path / "missing", tmp_path / "dest", required=True)


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("app/dist/launcher.js", "launcher.js"),
        ("app/dist/self-test.js", "self-test.js"),
        ("app/node_modules/@vibesys/backend-client/dist/index.js", "backend-client"),
        ("app/node_modules/@vibesys/core-state/dist/index.js", "core-state"),
        ("licenses/BUN-LICENSE.md", "BUN-LICENSE"),
        ("licenses/opentui-core.txt", "opentui-core"),
    ],
)
def test_required_staging_rejects_missing_files(tmp_path, relative_path, message):  # noqa: ANN001, ANN201
    source = _write_payload(tmp_path / "source")
    (source / relative_path).unlink()

    with pytest.raises(TuiPackagingError, match=message):
        stage_prebuilt_tui(source, tmp_path / "dest", required=True)


def test_staging_rejects_a_non_executable_bun(tmp_path):  # noqa: ANN001, ANN201
    source = _write_payload(tmp_path / "source")
    (source / "bin" / "bun").chmod(0o644)

    with pytest.raises(TuiPackagingError, match="executable"):
        stage_prebuilt_tui(source, tmp_path / "dest", required=True)


def test_staging_rejects_a_wrong_bun_version_or_target(tmp_path):  # noqa: ANN001, ANN201
    source = _write_payload(tmp_path / "source")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["bun_version"] = "1.3.8"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(TuiPackagingError, match="Bun version"):
        stage_prebuilt_tui(source, tmp_path / "dest", required=True)

    source = _write_payload(tmp_path / "other", target="macos-arm64")
    with pytest.raises(TuiPackagingError, match="target"):
        stage_prebuilt_tui(
            source,
            tmp_path / "dest",
            required=True,
            expected_target="linux-x86_64",
        )


def test_staging_rejects_an_unexpected_native_package(tmp_path):  # noqa: ANN001, ANN201
    source = _write_payload(tmp_path / "source")
    extra = source / "app" / "node_modules" / "@opentui" / "core-darwin-arm64"
    extra.mkdir(parents=True)
    (extra / "index.js").write_text("// wrong native package\n")

    with pytest.raises(TuiPackagingError, match="native package"):
        stage_prebuilt_tui(source, tmp_path / "dest", required=True)


def test_staging_rejects_source_maps_and_hash_mismatches(tmp_path):  # noqa: ANN001, ANN201
    source = _write_payload(tmp_path / "source")
    (source / "app" / "dist" / "launcher.js.map").write_text("{}\n")

    with pytest.raises(TuiPackagingError, match="source map"):
        stage_prebuilt_tui(source, tmp_path / "dest", required=True)

    (source / "app" / "dist" / "launcher.js.map").unlink()
    (source / "app" / "dist" / "launcher.js").write_text("// modified\n")
    with pytest.raises(TuiPackagingError, match="hash"):
        stage_prebuilt_tui(source, tmp_path / "dest", required=True)
