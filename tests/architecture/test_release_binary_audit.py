"""Unit-testable native binary compatibility policy."""

from __future__ import annotations

import pytest
from audit_release_wheel import (
    BinaryAuditError,
    parse_lipo_architectures,
    parse_macos_build_versions,
    parse_macos_dylib_dependencies,
    parse_macos_rpaths,
    parse_readelf_machine,
    parse_readelf_needed,
    parse_symbol_versions,
    validate_linux_dependencies,
    validate_linux_symbol_versions,
    validate_macos_build_versions,
    validate_macos_dependencies,
)

READELF_HEADER_X64 = """\
ELF Header:
  Class:                             ELF64
  Machine:                           Advanced Micro Devices X86-64
"""

READELF_DYNAMIC = """\
Dynamic section at offset 0x2d90 contains 27 entries:
 0x0000000000000001 (NEEDED)             Shared library: [libm.so.6]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
"""

READELF_VERSIONS = """\
  004:   2 (GLIBC_2.2.5)   3 (GLIBC_2.17)    4 (GLIBC_2.28)
  0x0010:   Name: GLIBC_2.25  Flags: none  Version: 5
  0x0020:   Name: GLIBCXX_3.4.24  Flags: none  Version: 6
  0x0030:   Name: CXXABI_1.3.11  Flags: none  Version: 7
  0x0040:   Name: GCC_7.0.0  Flags: none  Version: 8
"""

OTOOL_LOAD_COMMANDS = """\
Load command 8
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 13.0
      sdk 15.4
Load command 9
      cmd LC_RPATH
  cmdsize 40
     path @loader_path/../Frameworks (offset 12)
Load command 10
      cmd LC_LOAD_DYLIB
  cmdsize 56
     name /usr/lib/libSystem.B.dylib (offset 24)
Load command 11
      cmd LC_LOAD_DYLIB
  cmdsize 56
     name @loader_path/libhelper.dylib (offset 24)
Load command 12
      cmd LC_LOAD_WEAK_DYLIB
  cmdsize 56
     name @rpath/liboptional.dylib (offset 24)
"""


def test_parse_linux_native_tool_output() -> None:
    assert parse_readelf_machine(READELF_HEADER_X64) == "Advanced Micro Devices X86-64"
    assert parse_readelf_needed(READELF_DYNAMIC) == {"libm.so.6", "libc.so.6"}
    assert parse_symbol_versions(READELF_VERSIONS) == {
        "GLIBC": {"2.2.5", "2.17", "2.25", "2.28"},
        "GLIBCXX": {"3.4.24"},
        "CXXABI": {"1.3.11"},
        "GCC": {"7.0.0"},
    }


@pytest.mark.parametrize(
    "symbol",
    ["GLIBC_2.29", "GLIBCXX_3.4.25", "CXXABI_1.3.12", "GCC_8.0.0"],
)
def test_linux_audit_rejects_symbol_versions_outside_manylinux_2_28(symbol: str) -> None:
    family, version = symbol.split("_", maxsplit=1)

    with pytest.raises(BinaryAuditError, match=rf"{symbol}"):
        validate_linux_symbol_versions({family: {version}}, target_key="linux-x86_64")


def test_linux_audit_parses_underscore_symbol_suffixes_before_policy_validation() -> None:
    versions = parse_symbol_versions(
        "Name: CXXABI_TM_1  Flags: none\nName: GLIBC_ABI_DT_RELR  Flags: none\n"
    )

    assert versions == {"CXXABI": {"TM_1"}, "GLIBC": {"ABI_DT_RELR"}}
    validate_linux_symbol_versions(
        {"CXXABI": {"TM_1"}},
        target_key="linux-x86_64",
    )
    with pytest.raises(BinaryAuditError, match="GLIBC_ABI_DT_RELR"):
        validate_linux_symbol_versions(
            {"GLIBC": {"ABI_DT_RELR"}},
            target_key="linux-x86_64",
        )


def test_linux_audit_rejects_every_non_policy_dependency() -> None:
    with pytest.raises(BinaryAuditError, match=r"libssl\.so\.3"):
        validate_linux_dependencies(
            {"libc.so.6", "libssl.so.3"},
            target_key="linux-x86_64",
        )

    with pytest.raises(BinaryAuditError, match=r"libvibesys-helper\.so"):
        validate_linux_dependencies(
            {"libc.so.6", "libvibesys-helper.so"},
            target_key="linux-x86_64",
        )


def test_linux_audit_allows_only_the_targets_dynamic_loader() -> None:
    validate_linux_dependencies(
        {"libc.so.6", "ld-linux-x86-64.so.2"},
        target_key="linux-x86_64",
    )
    with pytest.raises(BinaryAuditError, match=r"ld-linux-aarch64\.so\.1"):
        validate_linux_dependencies(
            {"libc.so.6", "ld-linux-aarch64.so.1"},
            target_key="linux-x86_64",
        )


def test_parse_macos_native_tool_output() -> None:
    assert parse_lipo_architectures("x86_64 arm64\n") == {"x86_64", "arm64"}
    assert parse_lipo_architectures("arm64\n") == {"arm64"}
    assert parse_macos_build_versions(OTOOL_LOAD_COMMANDS) == {("macos", (13, 0))}
    assert parse_macos_rpaths(OTOOL_LOAD_COMMANDS) == {"@loader_path/../Frameworks"}
    assert parse_macos_dylib_dependencies(OTOOL_LOAD_COMMANDS) == {
        "/usr/lib/libSystem.B.dylib",
        "@loader_path/libhelper.dylib",
        "@rpath/liboptional.dylib",
    }


def test_macos_audit_rejects_deployment_targets_above_macos_13() -> None:
    validate_macos_build_versions({("macos", (12, 0)), ("macos", (13, 0))})
    with pytest.raises(BinaryAuditError, match=r"14\.0"):
        validate_macos_build_versions({("macos", (13, 0)), ("macos", (14, 0))})


def test_macos_audit_requires_lc_build_version_for_macos() -> None:
    with pytest.raises(BinaryAuditError, match="LC_BUILD_VERSION"):
        validate_macos_build_versions(set())
    with pytest.raises(BinaryAuditError, match="platform macOS"):
        validate_macos_build_versions({("ios", (13, 0))})


def test_macos_audit_resolves_only_system_or_wheel_contained_dependencies() -> None:
    member = "entrypoints/_tui/app/native/addon.node"
    contained = {
        member,
        "entrypoints/_tui/app/native/libhelper.dylib",
        "entrypoints/_tui/app/Frameworks/liboptional.dylib",
    }
    validate_macos_dependencies(
        {
            "/usr/lib/libSystem.B.dylib",
            "@loader_path/libhelper.dylib",
            "@rpath/liboptional.dylib",
        },
        member=member,
        rpaths={"@loader_path/../Frameworks"},
        wheel_binaries=contained,
    )

    with pytest.raises(BinaryAuditError, match=r"/opt/homebrew/lib/libssl\.3\.dylib"):
        validate_macos_dependencies(
            {"/opt/homebrew/lib/libssl.3.dylib"},
            member=member,
            rpaths=set(),
            wheel_binaries=contained,
        )
    with pytest.raises(BinaryAuditError, match="libmissing"):
        validate_macos_dependencies(
            {"@loader_path/libmissing.dylib"},
            member=member,
            rpaths=set(),
            wheel_binaries=contained,
        )
