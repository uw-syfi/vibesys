"""Build and verify the vibesys-evaluator platform wheels, which bundle the Go binaries.

``vibesys-evaluator`` is Python wrappers over a Go core. The release ships, like ``ruff``, one wheel
per platform that holds the three binaries (``vibesys-evaluator``, ``loadgen``, ``otelcapture``)
under ``vibesys_evaluator/_bin/``, where ``_private/core.py`` looks first, plus one source
distribution. There is no ``py3-none-any`` wheel: an installer on a platform without a wheel falls
back to the sdist, which has no binary, and the package then says to set ``$VIBESYS_EVALUATOR_BIN``.

Every binary is built with ``CGO_ENABLED=0``, so it is static: the one Linux wheel per CPU
architecture carries the manylinux and the musllinux tags at once, and the macOS wheels are
cross-compiled from Linux. Go 1.25 runs on macOS 12 and later.

Commands:

``build --out DIR --go-version X``  build the sdist, then the platform wheels, into DIR
``verify DIR --version V``          check the wheels of DIR (hashes, binaries, platform, tags)
``names --version V``               print the file names a release holds, one per line
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "sdk" / "vibesys-evaluator"
GO_ROOT = PACKAGE_ROOT / "go"
DISTRIBUTION = "vibesys_evaluator"
PROJECT = "vibesys-evaluator"
BIN_DIR = f"{DISTRIBUTION}/_bin"
BINARIES = ("vibesys-evaluator", "loadgen", "otelcapture")
VERSION_VAR = "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/version.Version"
# 1980-01-01, the earliest time a zip entry can carry: wheels are the same bytes at any time.
_DEFAULT_EPOCH = 315532800


@dataclass(frozen=True)
class Platform:
    """One platform wheel: the Go target and the wheel's platform tag."""

    name: str
    goos: str
    goarch: str
    # The platform part of the wheel tag; dots join the compatible tags of one wheel.
    tag: str


PLATFORMS = (
    Platform(
        "linux-x86_64",
        "linux",
        "amd64",
        "manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_2_x86_64",
    ),
    Platform(
        "linux-aarch64",
        "linux",
        "arm64",
        "manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_2_aarch64",
    ),
    Platform("macos-x86_64", "darwin", "amd64", "macosx_12_0_x86_64"),
    Platform("macos-arm64", "darwin", "arm64", "macosx_12_0_arm64"),
)
#: The platform whose wheel the task images install (they are linux/amd64).
IMAGE_PLATFORM = PLATFORMS[0]


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        msg = f"{name} is not on PATH"
        raise WheelError(msg)
    return found


class WheelError(RuntimeError):
    """A wheel does not hold what a release promises."""


def wheel_name(version: str, platform: Platform) -> str:
    """The file name of ``platform``'s wheel (PEP 427)."""
    return f"{DISTRIBUTION}-{version}-py3-none-{platform.tag}.whl"


def sdist_name(version: str) -> str:
    """The file name of the source distribution."""
    return f"{DISTRIBUTION}-{version}.tar.gz"


def release_names(version: str) -> set[str]:
    """Every file a release of ``version`` holds."""
    return {wheel_name(version, p) for p in PLATFORMS} | {sdist_name(version)}


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={digest}"


def retag(
    pure: Path, binaries: dict[str, Path], platform: Platform, out_dir: Path, epoch: int
) -> Path:
    """Make ``platform``'s wheel of the pure wheel ``pure`` and the binaries (name -> file)."""
    stamp = _zip_time(epoch)
    entries: list[tuple[str, bytes, int]] = []
    with zipfile.ZipFile(pure) as source:
        names = source.namelist()
        if any(n.startswith(f"{BIN_DIR}/") for n in names):
            msg = f"{pure.name} already holds {BIN_DIR}/: build from a clean source tree"
            raise WheelError(msg)
        dist_info = _dist_info(names)
        for info in source.infolist():
            if info.is_dir() or info.filename == f"{dist_info}/RECORD":
                continue
            data = source.read(info.filename)
            if info.filename == f"{dist_info}/WHEEL":
                data = _platform_wheel_file(data.decode(), platform).encode()
            entries.append((info.filename, data, 0o644))
    entries.extend((f"{BIN_DIR}/{name}", binaries[name].read_bytes(), 0o755) for name in BINARIES)
    entries.sort(key=lambda e: (e[0].startswith(dist_info), e[0]))
    version = dist_info.removeprefix(f"{DISTRIBUTION}-").removesuffix(".dist-info")
    record = "".join(f"{n},{_record_hash(d)},{len(d)}\n" for n, d, _ in entries)
    record += f"{dist_info}/RECORD,,\n"
    entries.append((f"{dist_info}/RECORD", record.encode(), 0o644))
    out = out_dir / wheel_name(version, platform)
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.create_system = 3
            target.writestr(info, data)
    return out


def _zip_time(epoch: int) -> tuple[int, int, int, int, int, int]:
    t = time.gmtime(max(epoch, _DEFAULT_EPOCH))
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec & ~1)


def _dist_info(names: list[str]) -> str:
    roots = {n.split("/", 1)[0] for n in names if n.split("/", 1)[0].endswith(".dist-info")}
    if len(roots) != 1:
        msg = f"expected one .dist-info directory, found {sorted(roots)}"
        raise WheelError(msg)
    return roots.pop()


def _platform_wheel_file(text: str, platform: Platform) -> str:
    """The WHEEL file of a pure wheel, made the platform wheel's."""
    lines = [line for line in text.splitlines() if line and not line.startswith("Tag:")]
    lines = [
        "Root-Is-Purelib: false" if line.startswith("Root-Is-Purelib:") else line for line in lines
    ]
    if "Root-Is-Purelib: false" not in lines:
        lines.append("Root-Is-Purelib: false")
    lines += [f"Tag: py3-none-{tag}" for tag in platform.tag.split(".")]
    return "\n".join(lines) + "\n"


def build_binaries(platform: Platform, out_dir: Path, go_version: str) -> dict[str, Path]:
    """Cross-compile the three binaries for ``platform`` (static, stamped with ``go_version``)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CGO_ENABLED": "0", "GOOS": platform.goos, "GOARCH": platform.goarch}
    # lint-waiver: LW-439563 [S603]; argv is the go on PATH building this repository's own module.
    subprocess.run(  # noqa: S603
        [
            _tool("go"),
            "build",
            "-trimpath",
            "-ldflags",
            f"-s -w -X {VERSION_VAR}={go_version}",
            "-o",
            f"{out_dir}/",
            *(f"./cmd/{name}" for name in BINARIES),
        ],
        cwd=GO_ROOT,
        env=env,
        check=True,
    )
    return {name: out_dir / name for name in BINARIES}


def build(out: Path, go_version: str, epoch: int) -> list[Path]:
    """Build the sdist and every platform wheel into ``out`` (which must be empty or new)."""
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        msg = f"{out} is not empty"
        raise WheelError(msg)
    built: list[Path] = []
    # setuptools builds in the source tree and reuses what a previous build left (stale modules
    # included); start from nothing and leave nothing.
    scratch = [
        PACKAGE_ROOT / "python" / "build",
        *(PACKAGE_ROOT / "python" / "src").glob("*.egg-info"),
    ]
    for path in scratch:
        shutil.rmtree(path, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        env = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
        # lint-waiver: LW-258176 [S603]; argv is the uv on PATH building this repository's own package.
        subprocess.run(  # noqa: S603
            [_tool("uv"), "build", "--package", PROJECT, "--sdist", "--out-dir", str(out)],
            cwd=REPO_ROOT,
            env=env,
            check=True,
        )
        # lint-waiver: LW-514002 [S603]; argv is the uv on PATH building this repository's own package.
        subprocess.run(  # noqa: S603
            [
                _tool("uv"),
                "build",
                "--package",
                PROJECT,
                "--wheel",
                "--out-dir",
                str(work / "pure"),
            ],
            cwd=REPO_ROOT,
            env=env,
            check=True,
        )
        (pure,) = (work / "pure").glob("*.whl")
        for platform in PLATFORMS:
            binaries = build_binaries(platform, work / "bin" / platform.name, go_version)
            built.append(retag(pure, binaries, platform, out, epoch))
    for path in scratch:
        shutil.rmtree(path, ignore_errors=True)
    return [*sorted(out.glob("*.tar.gz")), *built]


# --- verification -----------------------------------------------------------------------------

ELF_MACHINE = {"amd64": 62, "arm64": 183}
MACHO_CPU = {"amd64": 0x01000007, "arm64": 0x0100000C}
_ELF_64_LE = (2, 1)
_MACHO_64_LE = 0xFEEDFACF
_PT_INTERP = 3
_PT_DYNAMIC = 2


def check_binary(data: bytes, platform: Platform) -> None:
    """Raise WheelError unless ``data`` is a static executable of ``platform``."""
    if platform.goos == "linux":
        _check_elf(data, platform)
    else:
        _check_macho(data, platform)


def _check_elf(data: bytes, platform: Platform) -> None:
    if data[:4] != b"\x7fELF" or (data[4], data[5]) != _ELF_64_LE:  # 64-bit little endian
        msg = "not a 64-bit little-endian ELF file"
        raise WheelError(msg)
    (machine,) = struct.unpack_from("<H", data, 18)
    if machine != ELF_MACHINE[platform.goarch]:
        msg = f"ELF machine {machine}, not {ELF_MACHINE[platform.goarch]} ({platform.goarch})"
        raise WheelError(msg)
    (phoff,) = struct.unpack_from("<Q", data, 32)
    phentsize, phnum = struct.unpack_from("<HH", data, 54)
    for i in range(phnum):
        (kind,) = struct.unpack_from("<I", data, phoff + i * phentsize)
        if kind in {_PT_INTERP, _PT_DYNAMIC}:
            msg = "dynamically linked (has an interpreter or a dynamic section); build with CGO_ENABLED=0"
            raise WheelError(msg)


def _check_macho(data: bytes, platform: Platform) -> None:
    magic, cpu = struct.unpack_from("<II", data, 0)
    if magic != _MACHO_64_LE:  # 64-bit little-endian Mach-O
        msg = "not a 64-bit Mach-O file"
        raise WheelError(msg)
    if cpu != MACHO_CPU[platform.goarch]:
        msg = f"Mach-O cpu {cpu:#x}, not {MACHO_CPU[platform.goarch]:#x} ({platform.goarch})"
        raise WheelError(msg)


def _check_record(wheel: Path, archive: zipfile.ZipFile, dist_info: str) -> None:
    recorded = {}
    for line in archive.read(f"{dist_info}/RECORD").decode().splitlines():
        path, digest, size = line.rsplit(",", 2)
        recorded[path] = (digest, size)
    names = archive.namelist()
    if set(recorded) != set(names):
        msg = f"{wheel.name}: RECORD and the archive differ on {sorted(set(names) ^ set(recorded))}"
        raise WheelError(msg)
    for name in names:
        if name == f"{dist_info}/RECORD":
            continue
        data = archive.read(name)
        if recorded[name] != (_record_hash(data), str(len(data))):
            msg = f"{wheel.name}: RECORD is wrong for {name}"
            raise WheelError(msg)


def _check_binaries(wheel: Path, archive: zipfile.ZipFile, platform: Platform) -> None:
    bundled = {n for n in archive.namelist() if n.startswith(f"{BIN_DIR}/")}
    if bundled != {f"{BIN_DIR}/{b}" for b in BINARIES}:
        msg = f"{wheel.name}: {BIN_DIR}/ holds {sorted(bundled)}"
        raise WheelError(msg)
    for name in BINARIES:
        info = archive.getinfo(f"{BIN_DIR}/{name}")
        if not (info.external_attr >> 16) & stat.S_IXUSR:
            msg = f"{wheel.name}: {name} is not executable"
            raise WheelError(msg)
        try:
            check_binary(archive.read(info), platform)
        except WheelError as error:
            msg = f"{wheel.name}: {name}: {error}"
            raise WheelError(msg) from error


def verify_wheel(wheel: Path, platform: Platform, version: str) -> None:
    """Check one platform wheel: name, tags, RECORD hashes, and the three binaries."""
    if wheel.name != wheel_name(version, platform):
        msg = f"{wheel.name} is not {wheel_name(version, platform)}"
        raise WheelError(msg)
    with zipfile.ZipFile(wheel) as archive:
        dist_info = _dist_info(archive.namelist())
        wheel_file = archive.read(f"{dist_info}/WHEEL").decode()
        tags = re.findall(r"^Tag: (\S+)$", wheel_file, flags=re.MULTILINE)
        if tags != [f"py3-none-{tag}" for tag in platform.tag.split(".")]:
            msg = f"{wheel.name}: WHEEL tags {tags} do not match the file name"
            raise WheelError(msg)
        if "Root-Is-Purelib: false" not in wheel_file:
            msg = f"{wheel.name}: WHEEL must say Root-Is-Purelib: false"
            raise WheelError(msg)
        _check_record(wheel, archive, dist_info)
        _check_binaries(wheel, archive, platform)


def verify_dir(directory: Path, version: str) -> None:
    """Check that ``directory`` holds exactly a release of ``version`` and that it is sound."""
    actual = {p.name for p in directory.iterdir() if p.is_file() and p.name != ".gitignore"}
    expected = release_names(version)
    if actual != expected:
        msg = f"expected {sorted(expected)}, found {sorted(actual)}"
        raise WheelError(msg)
    for platform in PLATFORMS:
        verify_wheel(directory / wheel_name(version, platform), platform, version)


def _epoch() -> int:
    env = os.environ.get("SOURCE_DATE_EPOCH")
    if env:
        return int(env)
    # lint-waiver: LW-782554 [S603]; a fixed git command in this repository.
    done = subprocess.run(  # noqa: S603
        [_tool("git"), "log", "-1", "--format=%ct"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return int(done.stdout.strip() or _DEFAULT_EPOCH)


def main(argv: list[str] | None = None) -> int:
    """Run one command."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--go-version", required=True, help="stamped into the binaries (0.2.0-rc.1)")
    v = sub.add_parser("verify")
    v.add_argument("directory", type=Path)
    v.add_argument("--version", required=True)
    n = sub.add_parser("names")
    n.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            for path in build(args.out, args.go_version, _epoch()):
                print(f"{path} ({path.stat().st_size / 1e6:.1f} MB)")
        elif args.command == "verify":
            verify_dir(args.directory, args.version)
        else:
            print("\n".join(sorted(release_names(args.version))))
    except (WheelError, subprocess.CalledProcessError) as error:
        print(f"evaluator wheels: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
