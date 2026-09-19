"""Audit every embedded ELF or Mach-O in one native release wheel."""

from __future__ import annotations

import argparse
import json
import platform
import posixpath
import re
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Never, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wheel_targets import TARGETS, WheelTarget, resolve_wheel_target  # noqa: E402

Runner = Callable[..., subprocess.CompletedProcess[str]]
_POLICY_PATH = REPO_ROOT / "packaging" / "manylinux_2_28-policy.json"
_VERSIONED_SYMBOL_PATTERN = re.compile(
    r"\b(GLIBCXX|GLIBC|CXXABI|GCC|LIBATOMIC|ZLIB)_([A-Za-z0-9_.]+)\b"
)
_MACHO_MAGICS = frozenset(
    {
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)
_LINUX_MACHINES = {
    "linux-x86_64": "Advanced Micro Devices X86-64",
    "linux-aarch64": "AArch64",
}
_MACOS_ARCHES = {"macos-arm64": "arm64"}
_MACOS_SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")
_MACHO_DYLIB_COMMANDS = frozenset(
    {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB", "LC_LOAD_UPWARD_DYLIB"}
)


class BinaryAuditError(RuntimeError):
    """Raised when an embedded native binary violates compatibility policy."""

    @classmethod
    def unreadable_wheel(cls, wheel: Path, cause: object) -> BinaryAuditError:
        """Build an error for a wheel that cannot be inspected."""
        return cls(f"Cannot audit wheel {wheel}: {cause}")

    @classmethod
    def command_failed(cls, command: Sequence[str]) -> BinaryAuditError:
        """Build an error for a failed native inspection command."""
        return cls(f"Native audit command failed: {' '.join(command)}")


def parse_readelf_machine(output: str) -> str:
    """Extract the ELF machine value from ``readelf -h`` output."""
    match = re.search(r"^\s*Machine:\s*(.+?)\s*$", output, re.MULTILINE)
    if match is None:
        _fail("readelf output does not declare Machine")
    return match.group(1)


def parse_readelf_needed(output: str) -> set[str]:
    """Extract DT_NEEDED library basenames from ``readelf -d`` output."""
    return set(re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", output))


def parse_symbol_versions(output: str) -> dict[str, set[str]]:
    """Extract versioned ABI symbol families from ``readelf --version-info``."""
    versions: dict[str, set[str]] = {}
    for family, version in _VERSIONED_SYMBOL_PATTERN.findall(output):
        versions.setdefault(family, set()).add(version)
    return versions


def validate_linux_symbol_versions(
    versions: dict[str, set[str]],
    *,
    target_key: str,
) -> None:
    """Reject ABI symbols outside the pinned manylinux_2_28 policy."""
    policy = _manylinux_policy()
    raw_symbol_versions = policy.get("symbol_versions")
    if not isinstance(raw_symbol_versions, dict):
        _fail("manylinux policy has invalid symbol_versions")
    target_policy = raw_symbol_versions.get(target_key)
    if not isinstance(target_policy, dict):
        _fail(f"manylinux policy has no symbol versions for {target_key}")
    rejected: list[str] = []
    for family, referenced in versions.items():
        allowed = target_policy.get(family, [])
        if not isinstance(allowed, list):
            _fail(f"manylinux policy has invalid {family} symbol versions for {target_key}")
        rejected.extend(f"{family}_{version}" for version in referenced - set(allowed))
    if rejected:
        _fail(
            "ELF references symbols outside the auditwheel 6.8.0 manylinux_2_28 policy: "
            + ", ".join(sorted(rejected))
        )


def validate_linux_dependencies(libraries: set[str], *, target_key: str) -> None:
    """Require every ELF dependency to be allowed by the pinned policy."""
    policy = _manylinux_policy()
    raw_allowed = policy["allowed_system_libraries"]
    if not isinstance(raw_allowed, list):
        _fail("manylinux policy has invalid allowed_system_libraries")
    allowed = {library for library in raw_allowed if isinstance(library, str)}
    raw_architecture_libraries = policy.get("allowed_architecture_libraries")
    if not isinstance(raw_architecture_libraries, dict):
        _fail("manylinux policy has invalid allowed_architecture_libraries")
    architecture_libraries = raw_architecture_libraries.get(target_key)
    if not isinstance(architecture_libraries, list) or not all(
        isinstance(library, str) for library in architecture_libraries
    ):
        _fail(f"manylinux policy has invalid architecture libraries for {target_key}")
    allowed.update(architecture_libraries)
    unexpected = sorted(libraries - allowed)
    if unexpected:
        _fail(f"ELF dependencies are not allowed by the manylinux_2_28 policy: {unexpected}")


def parse_lipo_architectures(output: str) -> set[str]:
    """Extract the bare architecture tokens emitted by ``lipo -archs``."""
    architectures = output.split()
    if not architectures or any(
        re.fullmatch(r"[A-Za-z0-9_]+", item) is None for item in architectures
    ):
        _fail("lipo -archs output does not contain bare architecture tokens")
    return set(architectures)


def parse_macos_build_versions(output: str) -> set[tuple[str, tuple[int, ...]]]:
    """Extract LC_BUILD_VERSION platforms and minimum OS versions."""
    builds: set[tuple[str, tuple[int, ...]]] = set()
    command: str | None = None
    build_platform: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("cmd "):
            command = line.removeprefix("cmd ")
            build_platform = None
            continue
        if command != "LC_BUILD_VERSION":
            continue
        platform_match = re.fullmatch(r"platform\s+(\S+)", line)
        if platform_match is not None:
            raw_platform = platform_match.group(1).lower()
            build_platform = "macos" if raw_platform in {"1", "macos"} else raw_platform
            continue
        match = re.fullmatch(r"minos\s+(\d+(?:\.\d+)+)", line)
        if match is not None:
            if build_platform is None:
                _fail("LC_BUILD_VERSION does not declare a platform before minos")
            builds.add((build_platform, tuple(int(value) for value in match.group(1).split("."))))
    return builds


def validate_macos_build_versions(builds: set[tuple[str, tuple[int, ...]]]) -> None:
    """Require LC_BUILD_VERSION for macOS 13 or older."""
    if not builds:
        _fail("Mach-O does not declare LC_BUILD_VERSION")
    platforms = {platform_name for platform_name, _version in builds}
    if platforms != {"macos"}:
        _fail(f"Mach-O LC_BUILD_VERSION must use platform macOS, found {sorted(platforms)}")
    too_new = sorted(version for _platform, version in builds if version > (13, 0))
    if too_new:
        rendered = ", ".join(".".join(map(str, version)) for version in too_new)
        _fail(f"Mach-O requires macOS newer than 13.0: {rendered}")


def parse_macos_rpaths(output: str) -> set[str]:
    """Extract LC_RPATH search paths from ``otool -l`` output."""
    return _parse_macos_named_load_commands(output, commands={"LC_RPATH"}, field="path")


def parse_macos_dylib_dependencies(output: str) -> set[str]:
    """Extract loaded dylib names from ``otool -l`` output."""
    return _parse_macos_named_load_commands(output, commands=_MACHO_DYLIB_COMMANDS, field="name")


def validate_macos_dependencies(
    dependencies: set[str],
    *,
    member: str,
    rpaths: set[str],
    wheel_binaries: set[str],
) -> None:
    """Require Mach-O dylibs to resolve to macOS or wheel-contained binaries."""
    rejected = sorted(
        dependency
        for dependency in dependencies
        if not _macos_dependency_resolves(
            dependency,
            member=member,
            rpaths=rpaths,
            wheel_binaries=wheel_binaries,
        )
    )
    if rejected:
        _fail(f"Mach-O dependencies are not system or wheel-contained libraries: {rejected}")


def audit_wheel(
    wheel: Path,
    target: WheelTarget,
    *,
    runner: Runner = subprocess.run,
) -> None:
    """Discover and audit every native binary in ``wheel`` on its target host."""
    resolve_wheel_target(
        target.key,
        host_system=platform.system(),
        host_machine=platform.machine(),
    )
    expected_kind = "elf" if target.system == "Linux" else "macho"
    discovered = 0
    try:
        with (
            zipfile.ZipFile(wheel.resolve()) as archive,
            tempfile.TemporaryDirectory(prefix=f"vibesys-audit-{target.key}-") as temporary,
        ):
            binaries: list[tuple[Path, str, str]] = []
            for index, info in enumerate(archive.infolist()):
                if info.is_dir():
                    continue
                content = archive.read(info)
                kind = _binary_kind(content)
                if kind is None:
                    continue
                if kind != expected_kind:
                    _fail(f"Wheel contains unexpected {kind} binary: {info.filename}")
                discovered += 1
                extracted = Path(temporary) / f"native-{index}"
                extracted.write_bytes(content)
                binaries.append((extracted, info.filename, kind))
            wheel_binaries = {member for _path, member, _kind in binaries}
            for extracted, member, kind in binaries:
                if kind == "elf":
                    _audit_elf(
                        extracted,
                        member,
                        target=target,
                        runner=runner,
                    )
                else:
                    _audit_macho(
                        extracted,
                        member,
                        target=target,
                        runner=runner,
                        wheel_binaries=wheel_binaries,
                    )
    except (OSError, zipfile.BadZipFile) as exc:
        raise BinaryAuditError.unreadable_wheel(wheel, exc) from exc
    if discovered == 0:
        _fail(f"Wheel contains no {expected_kind.upper()} binaries")


def _binary_kind(content: bytes) -> str | None:
    if content.startswith(b"\x7fELF"):
        return "elf"
    if content[:4] in _MACHO_MAGICS:
        return "macho"
    return None


def _audit_elf(
    path: Path,
    member: str,
    *,
    target: WheelTarget,
    runner: Runner,
) -> None:
    machine = parse_readelf_machine(_run(["readelf", "-W", "-h", str(path)], runner=runner))
    expected = _LINUX_MACHINES[target.key]
    if machine != expected:
        _fail(f"ELF {member} has machine {machine!r}, expected {expected!r}")
    dynamic = _run(["readelf", "-W", "-d", str(path)], runner=runner)
    validate_linux_dependencies(parse_readelf_needed(dynamic), target_key=target.key)
    versions = _run(["readelf", "-W", "--version-info", str(path)], runner=runner)
    validate_linux_symbol_versions(parse_symbol_versions(versions), target_key=target.key)


def _audit_macho(
    path: Path,
    member: str,
    *,
    target: WheelTarget,
    runner: Runner,
    wheel_binaries: set[str],
) -> None:
    architectures = parse_lipo_architectures(_run(["lipo", "-archs", str(path)], runner=runner))
    expected = {_MACOS_ARCHES[target.key]}
    if architectures != expected:
        _fail(
            f"Mach-O {member} has architectures {sorted(architectures)}, expected {sorted(expected)}"
        )
    load_commands = _run(["otool", "-l", str(path)], runner=runner)
    validate_macos_build_versions(parse_macos_build_versions(load_commands))
    validate_macos_dependencies(
        parse_macos_dylib_dependencies(load_commands),
        member=member,
        rpaths=parse_macos_rpaths(load_commands),
        wheel_binaries=wheel_binaries,
    )


def _manylinux_policy() -> dict[str, object]:
    try:
        policy = json.loads(_POLICY_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"Cannot load pinned manylinux policy {_POLICY_PATH}: {exc}")
    if not isinstance(policy, dict) or policy.get("policy") != "manylinux_2_28":
        _fail(f"Invalid pinned manylinux policy: {_POLICY_PATH}")
    return cast("dict[str, object]", policy)


def _parse_macos_named_load_commands(
    output: str,
    *,
    commands: set[str] | frozenset[str],
    field: str,
) -> set[str]:
    values: set[str] = set()
    command: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("cmd "):
            command = line.removeprefix("cmd ")
            continue
        if command not in commands:
            continue
        match = re.fullmatch(rf"{field}\s+(.+?)\s+\(offset\s+\d+\)", line)
        if match is not None:
            values.add(match.group(1))
    return values


def _macos_dependency_resolves(
    dependency: str,
    *,
    member: str,
    rpaths: set[str],
    wheel_binaries: set[str],
) -> bool:
    if dependency.startswith(_MACOS_SYSTEM_PREFIXES):
        return True
    if dependency.startswith("/"):
        return False
    suffix = dependency.removeprefix("@loader_path/")
    if suffix != dependency:
        resolved = _wheel_relative_path(PurePosixPath(member).parent.as_posix(), suffix)
        return resolved in wheel_binaries
    suffix = dependency.removeprefix("@rpath/")
    if suffix != dependency:
        return any(
            _rpath_dependency_resolves(
                rpath,
                suffix=suffix,
                member=member,
                wheel_binaries=wheel_binaries,
            )
            for rpath in rpaths
        )
    return False


def _rpath_dependency_resolves(
    rpath: str,
    *,
    suffix: str,
    member: str,
    wheel_binaries: set[str],
) -> bool:
    loader_suffix = rpath.removeprefix("@loader_path/")
    if loader_suffix != rpath:
        base = _wheel_relative_path(PurePosixPath(member).parent.as_posix(), loader_suffix)
        return _wheel_relative_path(base, suffix) in wheel_binaries
    return rpath.startswith(_MACOS_SYSTEM_PREFIXES)


def _wheel_relative_path(base: str, suffix: str) -> str:
    resolved = posixpath.normpath(posixpath.join(base, suffix))
    if resolved == ".." or resolved.startswith(("../", "/")):
        return ""
    return resolved


def _run(command: Sequence[str], *, runner: Runner) -> str:
    try:
        result = runner(list(command), check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BinaryAuditError.command_failed(command) from exc
    return result.stdout


def _fail(message: str) -> Never:
    raise BinaryAuditError(message)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    return parser.parse_args()


def main() -> int:
    """Audit the requested wheel with concise diagnostics."""
    args = _parse_args()
    try:
        audit_wheel(args.wheel, TARGETS[args.target])
    except (BinaryAuditError, ValueError) as exc:
        print(f"native binary audit failed: {exc}", file=sys.stderr)
        return 1
    print(f"audited {args.wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
