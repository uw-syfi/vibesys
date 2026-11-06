"""The vibesys-evaluator platform wheels: what they hold and what the verifier refuses."""

from __future__ import annotations

import stat
import struct
import zipfile
from typing import TYPE_CHECKING

import evaluator_wheels as ew
import pytest
from check_release_version import ReleaseVersionError, check_release_version

if TYPE_CHECKING:
    from pathlib import Path

VERSION = "0.2.0rc1"
DIST_INFO = f"vibesys_evaluator-{VERSION}.dist-info"


def _elf(machine: int, *, dynamic: bool = False) -> bytes:
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<Q", header, 32, 64)  # e_phoff
    struct.pack_into("<HH", header, 54, 56, 1)  # e_phentsize, e_phnum
    program = bytearray(56)
    struct.pack_into("<I", program, 0, 3 if dynamic else 1)  # PT_INTERP or PT_LOAD
    return bytes(header + program)


def _macho(cpu: int) -> bytes:
    return struct.pack("<II", 0xFEEDFACF, cpu) + bytes(24)


def _binary(platform: ew.Platform, *, dynamic: bool = False) -> bytes:
    if platform.goos == "linux":
        return _elf(ew.ELF_MACHINE[platform.goarch], dynamic=dynamic)
    return _macho(ew.MACHO_CPU[platform.goarch])


def _pure_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("vibesys_evaluator/__init__.py", "")
        archive.writestr(
            f"{DIST_INFO}/METADATA",
            f"Metadata-Version: 2.4\nName: vibesys-evaluator\nVersion: {VERSION}\n\n",
        )
        archive.writestr(
            f"{DIST_INFO}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: setuptools\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{DIST_INFO}/RECORD", "")
    return path


def _make(tmp_path: Path, platform: ew.Platform, *, dynamic: bool = False) -> Path:
    bins = tmp_path / "bin" / platform.name
    bins.mkdir(parents=True)
    for name in ew.BINARIES:
        (bins / name).write_bytes(_binary(platform, dynamic=dynamic))
    pure = _pure_wheel(tmp_path / "pure.whl")
    return ew.retag(
        pure, {n: bins / n for n in ew.BINARIES}, platform, tmp_path / "out", epoch=1_700_000_000
    )


@pytest.mark.parametrize("platform", ew.PLATFORMS, ids=lambda p: p.name)
def test_a_platform_wheel_carries_the_binaries_and_its_tags(
    tmp_path: Path, platform: ew.Platform
) -> None:
    wheel = _make(tmp_path, platform)
    assert wheel.name == ew.wheel_name(VERSION, platform)
    ew.verify_wheel(wheel, platform, VERSION)
    with zipfile.ZipFile(wheel) as archive:
        for name in ew.BINARIES:
            info = archive.getinfo(f"vibesys_evaluator/_bin/{name}")
            assert (info.external_attr >> 16) & stat.S_IXUSR
        wheel_file = archive.read(f"{DIST_INFO}/WHEEL").decode()
    assert "Root-Is-Purelib: false" in wheel_file
    assert "Tag: py3-none-any" not in wheel_file
    assert wheel_file.count("Tag: ") == len(platform.tag.split("."))


def test_the_linux_wheels_are_manylinux_and_musllinux_the_macos_ones_macosx() -> None:
    by_name = {p.name: p.tag for p in ew.PLATFORMS}
    for arch in ("x86_64", "aarch64"):
        tags = by_name[f"linux-{arch}"].split(".")
        assert {t.split("_")[0] for t in tags} == {"manylinux2014", "manylinux", "musllinux"}
        assert all(t.endswith(arch) for t in tags)
    assert by_name["macos-x86_64"].startswith("macosx_")
    assert by_name["macos-arm64"].endswith("_arm64")


def test_a_wheel_is_the_same_bytes_every_time(tmp_path: Path) -> None:
    platform = ew.PLATFORMS[0]
    first = _make(tmp_path / "a", platform).read_bytes()
    second = _make(tmp_path / "b", platform).read_bytes()
    assert first == second


def test_a_dynamically_linked_binary_is_refused(tmp_path: Path) -> None:
    platform = ew.PLATFORMS[0]
    wheel = _make(tmp_path, platform, dynamic=True)
    with pytest.raises(ew.WheelError, match="dynamically linked"):
        ew.verify_wheel(wheel, platform, VERSION)


def test_a_binary_of_another_architecture_is_refused(tmp_path: Path) -> None:
    linux_amd64, linux_arm64 = ew.PLATFORMS[0], ew.PLATFORMS[1]
    wheel = _make(tmp_path, linux_arm64)
    renamed = wheel.with_name(ew.wheel_name(VERSION, linux_amd64))
    wheel.rename(renamed)
    # The file name now says x86_64: the tags in WHEEL and the machine of the ELF both disagree.
    with pytest.raises(ew.WheelError, match="tags"):
        ew.verify_wheel(renamed, linux_amd64, VERSION)


def test_a_tampered_binary_fails_the_record(tmp_path: Path) -> None:
    platform = ew.PLATFORMS[2]
    wheel = _make(tmp_path, platform)
    forged = tmp_path / "forged.whl"
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(forged, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith("/loadgen"):
                data += b"x"
            dst.writestr(info, data)
    forged.rename(wheel)
    with pytest.raises(ew.WheelError, match="RECORD is wrong"):
        ew.verify_wheel(wheel, platform, VERSION)


def test_a_wheel_missing_a_binary_is_refused(tmp_path: Path) -> None:
    platform = ew.PLATFORMS[0]
    wheel = _make(tmp_path, platform)
    kept = tmp_path / "kept.whl"
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(kept, "w") as dst:
        for info in src.infolist():
            if not info.filename.endswith("/otelcapture") and not info.filename.endswith("RECORD"):
                dst.writestr(info, src.read(info))
        record = src.read(f"{DIST_INFO}/RECORD").decode()
        dst.writestr(
            f"{DIST_INFO}/RECORD",
            "".join(ln + "\n" for ln in record.splitlines() if "otelcapture" not in ln),
        )
    kept.rename(wheel)
    with pytest.raises(ew.WheelError, match=r"_bin/ holds"):
        ew.verify_wheel(wheel, platform, VERSION)


def test_a_release_is_one_wheel_per_platform_and_an_sdist() -> None:
    names = ew.release_names(VERSION)
    assert len(names) == len(ew.PLATFORMS) + 1
    assert f"vibesys_evaluator-{VERSION}.tar.gz" in names
    assert not any(n.endswith("py3-none-any.whl") for n in names)


def test_release_version_check_accepts_exactly_the_release_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "vibesys-evaluator"\nversion = "{VERSION}"\n'
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    for platform in ew.PLATFORMS:
        wheel = dist / ew.wheel_name(VERSION, platform)
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                f"{DIST_INFO}/METADATA",
                f"Metadata-Version: 2.4\nName: vibesys-evaluator\nVersion: {VERSION}\n\n",
            )
    (dist / ew.sdist_name(VERSION)).write_bytes(b"sdist")
    check_release_version(
        None, source_root=tmp_path, wheel_dir=dist, expected_files=ew.release_names
    )
    (dist / ew.sdist_name(VERSION)).unlink()
    with pytest.raises(ReleaseVersionError, match="exactly"):
        check_release_version(
            None, source_root=tmp_path, wheel_dir=dist, expected_files=ew.release_names
        )
