"""Contracts for statically verifying self-contained release wheels."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from scripts import verify_release_wheel as verifier
from wheel_targets import TARGETS

FRAMEWORK_PACKAGES = (
    "vibesys",
    "entrypoints",
    "server",
    "vs_correctness",
    "vs_evaluator_protocol",
    "vs_feature_flags",
    "vs_github",
    "vs_issue_board",
    "vs_loop_state",
    "vs_project",
    "vs_prompts",
    "vs_sandbox",
)
PLATLIB = ""
DIST_INFO = "vibesys-0.1.0.dist-info"


def _source_file(root: Path, relative: str, content: bytes = b"source\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _source_root(root: Path) -> Path:
    pyproject = """\
[project]
name = "vibesys"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["example>=1"]
"""
    _source_file(root, "pyproject.toml", pyproject.encode())
    roots = {
        "vibesys": "src/vibesys",
        "entrypoints": "src/entrypoints",
        "server": "src/server",
        "vs_correctness": "libs/vs-correctness/src/vs_correctness",
        "vs_evaluator_protocol": "libs/vs-evaluator-protocol/src/vs_evaluator_protocol",
        "vs_feature_flags": "libs/vs-feature-flags/src/vs_feature_flags",
        "vs_github": "libs/vs-github/src/vs_github",
        "vs_issue_board": "libs/vs-issue-board/src/vs_issue_board",
        "vs_loop_state": "libs/vs-loop-state/src/vs_loop_state",
        "vs_project": "libs/vs-project/src/vs_project",
        "vs_prompts": "libs/vs-prompts/src/vs_prompts",
        "vs_sandbox": "libs/vs-sandbox/src/vs_sandbox",
    }
    for package, source in roots.items():
        _source_file(root, f"{source}/__init__.py", f"PACKAGE = {package!r}\n".encode())
        if package != "vibesys":
            _source_file(root, f"{source}/py.typed", b"")

    _source_file(
        root,
        "resources/evaluators/queue/vibesys.evaluator.toml",
        b"schema_version = 1\n",
    )
    _source_file(root, "resources/profilers/nsys/server.py", b"# profiler\n")
    _source_file(root, "resources/skills/demo/SKILL.md", b"# demo skill\n")
    _source_file(root, "resources/skills/demo/scripts/check.py", b"# helper\n")
    _source_file(root, "sdk/vs-bench/pyproject.toml", b"[project]\nname='vs-bench'\n")
    _source_file(root, "sdk/vs-bench/README.md", b"# vs-bench\n")
    _source_file(root, "sdk/vs-bench/src/vs_bench/__init__.py", b"VALUE = 1\n")
    _source_file(root, "sdk/vs-bench/src/vs_bench/py.typed", b"")
    _source_file(root, "third_party/bun/LICENSE", b"Bun license\n")
    _source_file(root, "LICENSE", b"VibeSys license\n")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S607
    subprocess.run(["git", "add", "."], cwd=root, check=True)  # noqa: S607
    return root


def _packaged_source_files(source_root: Path) -> dict[str, bytes]:
    mappings = {
        "src/vibesys": "vibesys",
        "src/entrypoints": "entrypoints",
        "src/server": "server",
        "libs/vs-correctness/src/vs_correctness": "vs_correctness",
        "libs/vs-evaluator-protocol/src/vs_evaluator_protocol": "vs_evaluator_protocol",
        "libs/vs-feature-flags/src/vs_feature_flags": "vs_feature_flags",
        "libs/vs-github/src/vs_github": "vs_github",
        "libs/vs-issue-board/src/vs_issue_board": "vs_issue_board",
        "libs/vs-loop-state/src/vs_loop_state": "vs_loop_state",
        "libs/vs-project/src/vs_project": "vs_project",
        "libs/vs-prompts/src/vs_prompts": "vs_prompts",
        "libs/vs-sandbox/src/vs_sandbox": "vs_sandbox",
        "resources/evaluators": "vibesys/_resources/evaluators",
        "resources/profilers": "vibesys/_resources/profilers",
        "resources/skills": "vibesys/_resources/skills",
        "sdk/vs-bench": "vibesys/_sdk/vs-bench",
    }
    files: dict[str, bytes] = {}
    for source_prefix, archive_prefix in mappings.items():
        for path in (source_root / source_prefix).rglob("*"):
            if path.is_file():
                relative = path.relative_to(source_root / source_prefix).as_posix()
                files[f"{PLATLIB}{archive_prefix}/{relative}"] = path.read_bytes()
    return files


def _wheel_files(source_root: Path) -> dict[str, bytes]:
    files = _packaged_source_files(source_root)
    tui_files = {
        "bin/bun": b"#!/bin/sh\n",
        "app/dist/launcher.js": b"// launcher\n",
        "app/dist/self-test.js": b"// self-test\n",
        "app/package.json": b'{"name":"@vibesys/tui","version":"0.1.0"}\n',
        "app/node_modules/@vibesys/backend-client/package.json": b'{"name":"@vibesys/backend-client"}\n',
        "app/node_modules/@vibesys/backend-client/dist/index.js": b"// backend client\n",
        "app/node_modules/@vibesys/core-state/package.json": b'{"name":"@vibesys/core-state"}\n',
        "app/node_modules/@vibesys/core-state/dist/index.js": b"// core state\n",
        "app/node_modules/@opentui/core/index.js": b"// core\n",
        "app/node_modules/@opentui/core-linux-x64/index.js": b"// native\n",
        "licenses/BUN-LICENSE.md": b"Bun license\n",
        "licenses/opentui-core.txt": b"OpenTUI license\n",
    }
    tui_root = f"{PLATLIB}entrypoints/_tui"
    files.update({f"{tui_root}/{relative}": content for relative, content in tui_files.items()})
    manifest = {
        "schema_version": 1,
        "target": "linux-x86_64",
        "bun_version": "1.3.9",
        "tui_version": "0.1.0",
        "files": {
            relative: hashlib.sha256(content).hexdigest() for relative, content in tui_files.items()
        },
    }
    files[f"{tui_root}/manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode()
    files[f"{DIST_INFO}/METADATA"] = b"""\
Metadata-Version: 2.4
Name: vibesys
Version: 0.1.0
Requires-Python: >=3.12
Requires-Dist: example>=1

fixture
"""
    files[f"{DIST_INFO}/WHEEL"] = b"""\
Wheel-Version: 1.0
Generator: test
Root-Is-Purelib: false
Tag: py3-none-manylinux_2_28_x86_64
"""
    files[f"{DIST_INFO}/entry_points.txt"] = (
        b"[console_scripts]\n"
        b"vibesys = entrypoints.launcher:main\n"
        b"vibesys-issue-mcp = vs_issue_board.mcp:main\n"
    )
    files[f"{DIST_INFO}/top_level.txt"] = ("\n".join(FRAMEWORK_PACKAGES) + "\n").encode()
    files[f"{DIST_INFO}/licenses/LICENSE"] = (source_root / "LICENSE").read_bytes()
    files[f"{DIST_INFO}/RECORD"] = b""
    return files


def _refresh_payload_manifest(files: dict[str, bytes]) -> None:
    tui_root = f"{PLATLIB}entrypoints/_tui/"
    manifest_path = f"{tui_root}manifest.json"
    manifest = json.loads(files[manifest_path])
    manifest["files"] = {
        name.removeprefix(tui_root): hashlib.sha256(content).hexdigest()
        for name, content in files.items()
        if name.startswith(tui_root) and name != manifest_path
    }
    files[manifest_path] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def _write_wheel(
    path: Path,
    source_root: Path,
    *,
    remove: tuple[str, ...] = (),
    extra: dict[str, bytes] | None = None,
    executable_bun: bool = True,
) -> Path:
    files = _wheel_files(source_root)
    for name in remove:
        files.pop(name)
    if extra:
        files.update(extra)
    _refresh_payload_manifest(files)

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            mode = 0o755 if executable_bun and name == "entrypoints/_tui/bin/bun" else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, content)
    return path


@pytest.fixture
def release_fixture(tmp_path: Path) -> tuple[Path, Path]:
    source_root = _source_root(tmp_path / "source")
    wheel = _write_wheel(
        tmp_path / "vibesys-0.1.0-py3-none-manylinux_2_28_x86_64.whl",
        source_root,
    )
    return source_root, wheel


def _verify(source_root: Path, wheel: Path) -> None:
    verifier.verify_wheel(wheel, source_root, TARGETS["linux-x86_64"])


def _append_directory(wheel: Path, name: str) -> None:
    directory = zipfile.ZipInfo(name)
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(directory, b"")


def test_complete_spread_layout_wheel_passes(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture

    _verify(source_root, wheel)


def test_tolerates_canonical_zip_directory_entries(
    release_fixture: tuple[Path, Path],
) -> None:
    source_root, wheel = release_fixture
    _append_directory(wheel, f"{PLATLIB}vibesys/")

    _verify(source_root, wheel)


@pytest.mark.parametrize(
    ("directory_name", "message"),
    [
        ("unused-directory/", "unexpected directory"),
        (f"{PLATLIB}vibesys/__init__.py/", "aliases an allowed file"),
    ],
)
def test_rejects_invalid_canonical_zip_directory_entries(
    release_fixture: tuple[Path, Path],
    directory_name: str,
    message: str,
) -> None:
    source_root, wheel = release_fixture
    _append_directory(wheel, directory_name)

    with pytest.raises(verifier.ReleaseWheelError, match=message):
        _verify(source_root, wheel)


def test_rejects_file_and_directory_entry_collisions(
    release_fixture: tuple[Path, Path],
) -> None:
    source_root, wheel = release_fixture
    collision = f"{PLATLIB}vibesys"
    _write_wheel(wheel, source_root, extra={collision: b"file\n"})
    _append_directory(wheel, f"{collision}/")

    with pytest.raises(verifier.ReleaseWheelError, match="colliding archive entries"):
        _verify(source_root, wheel)


@pytest.mark.parametrize(
    "unexpected_name",
    [
        f"{PLATLIB}arbitrary_package/__init__.py",
        f"{PLATLIB}vibesys/secret.txt",
        "vibesys-9.9.9.dist-info/METADATA",
        "other/__init__.py",
    ],
)
def test_rejects_every_unexpected_wheel_member(
    release_fixture: tuple[Path, Path],
    unexpected_name: str,
) -> None:
    source_root, wheel = release_fixture
    _write_wheel(wheel, source_root, extra={unexpected_name: b"unexpected\n"})

    with pytest.raises(verifier.ReleaseWheelError, match="unexpected"):
        _verify(source_root, wheel)


@pytest.mark.parametrize(
    "alias",
    [
        f"{PLATLIB}vibesys/./__init__.py",
        f"{PLATLIB}vibesys//__init__.py",
    ],
)
def test_rejects_non_canonical_archive_aliases(
    release_fixture: tuple[Path, Path],
    alias: str,
) -> None:
    source_root, wheel = release_fixture
    _write_wheel(wheel, source_root, extra={alias: b"alias\n"})

    with pytest.raises(verifier.ReleaseWheelError, match="non-canonical"):
        _verify(source_root, wheel)


def test_rejects_a_universal_wheel_filename(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    universal = wheel.with_name("vibesys-0.1.0-py3-none-any.whl")
    wheel.rename(universal)

    with pytest.raises(verifier.ReleaseWheelError, match="platform"):
        _verify(source_root, universal)


def test_rejects_a_filename_version_mismatch(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    wrong_version = wheel.with_name("vibesys-0.2.0-py3-none-manylinux_2_28_x86_64.whl")
    wheel.rename(wrong_version)

    with pytest.raises(verifier.ReleaseWheelError, match=r"filename.*version"):
        _verify(source_root, wrong_version)


def test_rejects_a_missing_internal_package(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    missing = f"{PLATLIB}vs_sandbox/__init__.py"
    _write_wheel(wheel, source_root, remove=(missing,))

    with pytest.raises(verifier.ReleaseWheelError, match="vs_sandbox"):
        _verify(source_root, wheel)


def test_rejects_an_internal_distribution_dependency(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    metadata = _wheel_files(source_root)[f"{DIST_INFO}/METADATA"]
    metadata = metadata.replace(
        b"Requires-Dist: example>=1\n",
        b"Requires-Dist: example>=1\nRequires-Dist: vs-sandbox\n",
    )
    _write_wheel(wheel, source_root, extra={f"{DIST_INFO}/METADATA": metadata})

    with pytest.raises(verifier.ReleaseWheelError, match="vs-sandbox"):
        _verify(source_root, wheel)


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        (
            f"{PLATLIB}vibesys/_resources/evaluators/queue/vibesys.evaluator.toml",
            "resources/evaluators",
        ),
        (f"{PLATLIB}vibesys/_resources/skills/demo/SKILL.md", "resources/skills"),
        (f"{PLATLIB}vibesys/_sdk/vs-bench/pyproject.toml", "sdk/vs-bench"),
        (f"{PLATLIB}entrypoints/_tui/licenses/BUN-LICENSE.md", "BUN-LICENSE"),
    ],
)
def test_rejects_missing_release_content(
    release_fixture: tuple[Path, Path],
    missing: str,
    message: str,
) -> None:
    source_root, wheel = release_fixture
    _write_wheel(wheel, source_root, remove=(missing,))

    with pytest.raises(verifier.ReleaseWheelError, match=message):
        _verify(source_root, wheel)


def test_rejects_a_wrong_target_payload(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    files = _wheel_files(source_root)
    manifest_path = f"{PLATLIB}entrypoints/_tui/manifest.json"
    manifest = json.loads(files[manifest_path])
    manifest["target"] = "macos-arm64"
    files[manifest_path] = json.dumps(manifest).encode()
    _write_wheel(wheel, source_root, extra={manifest_path: files[manifest_path]})

    with pytest.raises(verifier.ReleaseWheelError, match="target"):
        _verify(source_root, wheel)


def test_rejects_checkout_specific_tui_dependencies(
    release_fixture: tuple[Path, Path],
) -> None:
    source_root, wheel = release_fixture
    files = _wheel_files(source_root)
    package_path = f"{PLATLIB}entrypoints/_tui/app/package.json"
    package = json.loads(files[package_path])
    package["dependencies"] = {
        "@vibesys/core-state": "@vibesys/core-state@file:///build/clients/core-state"
    }
    files[package_path] = json.dumps(package).encode()
    _write_wheel(wheel, source_root, extra={package_path: files[package_path]})

    with pytest.raises(verifier.ReleaseWheelError, match="local path"):
        _verify(source_root, wheel)


def test_rejects_multiple_native_packages(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    extra_native = f"{PLATLIB}entrypoints/_tui/app/node_modules/@opentui/core-darwin-arm64/index.js"
    _write_wheel(wheel, source_root, extra={extra_native: b"wrong native\n"})

    with pytest.raises(verifier.ReleaseWheelError, match="native package"):
        _verify(source_root, wheel)


def test_rejects_a_non_executable_bundled_runtime(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    _write_wheel(wheel, source_root, executable_bun=False)

    with pytest.raises(verifier.ReleaseWheelError, match="executable"):
        _verify(source_root, wheel)


def test_rejects_a_wheel_over_the_pypi_file_limit(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    archive_bytes = wheel.read_bytes()
    with wheel.open("wb") as stream:
        stream.seek(verifier.PYPI_FILE_SIZE_LIMIT + 1)
        stream.write(archive_bytes)

    assert wheel.stat().st_size > verifier.PYPI_FILE_SIZE_LIMIT
    with pytest.raises(verifier.ReleaseWheelError, match="100 MB"):
        _verify(source_root, wheel)


def test_rejects_a_metadata_version_mismatch(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    metadata = _wheel_files(source_root)[f"{DIST_INFO}/METADATA"].replace(
        b"Version: 0.1.0", b"Version: 0.2.0"
    )
    _write_wheel(wheel, source_root, extra={f"{DIST_INFO}/METADATA": metadata})

    with pytest.raises(verifier.ReleaseWheelError, match="version"):
        _verify(source_root, wheel)


def test_rejects_an_mcp_requirement_that_admits_version_2(
    release_fixture: tuple[Path, Path],
) -> None:
    source_root, wheel = release_fixture
    project = source_root / "pyproject.toml"
    project.write_text(project.read_text().replace("example>=1", "mcp>=1.0"))
    metadata = _wheel_files(source_root)[f"{DIST_INFO}/METADATA"].replace(
        b"Requires-Dist: example>=1", b"Requires-Dist: mcp>=1.0"
    )
    _write_wheel(wheel, source_root, extra={f"{DIST_INFO}/METADATA": metadata})

    with pytest.raises(verifier.ReleaseWheelError, match=r"MCP.*2\.0\.0"):
        _verify(source_root, wheel)


def test_rejects_a_source_digest_mismatch(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    archive_path = f"{PLATLIB}vibesys/__init__.py"
    _write_wheel(wheel, source_root, extra={archive_path: b"tampered\n"})

    with pytest.raises(verifier.ReleaseWheelError, match="digest"):
        _verify(source_root, wheel)


def test_rejects_a_tracked_packaging_source_missing_from_the_worktree(
    release_fixture: tuple[Path, Path],
) -> None:
    source_root, wheel = release_fixture
    missing = source_root / "resources" / "skills" / "demo" / "SKILL.md"
    missing.unlink()

    with pytest.raises(
        verifier.ReleaseWheelError, match=r"tracked source.*missing.*resources/skills"
    ):
        _verify(source_root, wheel)


@pytest.mark.parametrize(
    ("member", "replacement", "message"),
    [
        (
            f"{PLATLIB}entrypoints/_tui/licenses/BUN-LICENSE.md",
            b"substituted Bun license\n",
            "Bun license digest",
        ),
        (
            f"{DIST_INFO}/licenses/LICENSE",
            b"substituted root license\n",
            "root license digest",
        ),
    ],
)
def test_rejects_substituted_repository_owned_licenses(
    release_fixture: tuple[Path, Path],
    member: str,
    replacement: bytes,
    message: str,
) -> None:
    source_root, wheel = release_fixture
    _write_wheel(wheel, source_root, extra={member: replacement})

    with pytest.raises(verifier.ReleaseWheelError, match=message):
        _verify(source_root, wheel)


@pytest.mark.parametrize(
    "unsafe_name",
    ["/absolute", "C:/absolute", "../traversal", "src/vibesys/leak.py"],
)
def test_rejects_unsafe_or_repository_archive_paths(
    release_fixture: tuple[Path, Path],
    unsafe_name: str,
) -> None:
    source_root, wheel = release_fixture
    _write_wheel(wheel, source_root, extra={unsafe_name: b"unsafe\n"})

    with pytest.raises(verifier.ReleaseWheelError, match="archive path"):
        _verify(source_root, wheel)


@pytest.mark.parametrize(
    ("metadata_file", "header"),
    [
        ("METADATA", "Name"),
        ("METADATA", "Version"),
        ("METADATA", "Requires-Python"),
        ("WHEEL", "Root-Is-Purelib"),
        ("WHEEL", "Tag"),
    ],
)
def test_rejects_duplicate_singleton_metadata_headers(
    release_fixture: tuple[Path, Path],
    metadata_file: str,
    header: str,
) -> None:
    source_root, wheel = release_fixture
    member = f"{DIST_INFO}/{metadata_file}"
    content = _wheel_files(source_root)[member]
    line = next(
        line for line in content.splitlines(keepends=True) if line.startswith(f"{header}:".encode())
    )
    duplicated = content.replace(line, line + line, 1)
    _write_wheel(wheel, source_root, extra={member: duplicated})

    with pytest.raises(verifier.ReleaseWheelError, match=rf"exactly one {header}"):
        _verify(source_root, wheel)


@pytest.mark.parametrize(
    ("metadata_file", "header"),
    [("METADATA", "Metadata-Version"), ("WHEEL", "Wheel-Version")],
)
def test_rejects_unsupported_metadata_standard_versions(
    release_fixture: tuple[Path, Path],
    metadata_file: str,
    header: str,
) -> None:
    source_root, wheel = release_fixture
    member = f"{DIST_INFO}/{metadata_file}"
    content = _wheel_files(source_root)[member]
    unsupported = content.replace(f"{header}: 2.4".encode(), f"{header}: 9.9".encode())
    if header == "Wheel-Version":
        unsupported = content.replace(b"Wheel-Version: 1.0", b"Wheel-Version: 9.9")
    _write_wheel(wheel, source_root, extra={member: unsupported})

    with pytest.raises(verifier.ReleaseWheelError, match=rf"unsupported {header}"):
        _verify(source_root, wheel)


def test_rejects_duplicate_archive_names(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    duplicate = f"{PLATLIB}vibesys/__init__.py"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(duplicate, b"duplicate\n")

    with pytest.raises(verifier.ReleaseWheelError, match="duplicate"):
        _verify(source_root, wheel)


def test_rejects_symlinks_and_source_maps(release_fixture: tuple[Path, Path]) -> None:
    source_root, wheel = release_fixture
    symlink = zipfile.ZipInfo(f"{PLATLIB}entrypoints/_tui/link")
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(symlink, "bin/bun")
        archive.writestr(f"{PLATLIB}entrypoints/_tui/app/dist/launcher.js.map", b"{}")

    with pytest.raises(verifier.ReleaseWheelError, match=r"symlink|source map"):
        _verify(source_root, wheel)


def test_bdist_wheel_revalidates_the_native_host(tmp_path: Path) -> None:
    # The guard can only fire for a target the build host is not, so pick one
    # rather than naming a fixed target that happens to be foreign to CI. In
    # reverse declaration order the first non-native target is ``macos-arm64``
    # everywhere except an Apple Silicon Mac, so CI keeps its existing case.
    host = (platform.system(), platform.machine())
    target_key = next(
        key for key, target in reversed(TARGETS.items()) if (target.system, target.machine) != host
    )
    env = {**os.environ, "VIBESYS_WHEEL_TARGET": target_key}

    uv = shutil.which("uv")
    assert uv is not None
    result = subprocess.run(  # noqa: S603
        [uv, "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # Name the target as well: a bare "nonzero exit" also matches unrelated
    # build failures, which is how this assertion stayed green off-target.
    assert result.returncode != 0
    assert f"{target_key} must be built natively" in result.stderr
