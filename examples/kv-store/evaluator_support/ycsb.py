"""Checksum-pinned YCSB Redis binding install and runners."""

from __future__ import annotations

import fcntl
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

YCSB_VERSION = "0.17.0"
YCSB_URL = (
    f"https://github.com/brianfrankcooper/YCSB/releases/download/"
    f"{YCSB_VERSION}/ycsb-redis-binding-{YCSB_VERSION}.tar.gz"
)
YCSB_SHA256 = "353eb96c12a605c30c94928b85780ae4673578a21e2aa13782cd7f591991e484"

# Huge op-count cap so a run ends on maxexecutiontime, not on ops exhausted.
_OP_CAP = 1_000_000_000

# Metric key YCSB emits for overall throughput; the headline number.
THROUGHPUT_KEY = "OVERALL.Throughput(ops/sec)"

# Op types YCSB reports, and the CoreWorkload proportion knob that drives each
# (used by --probe-per-op to isolate one op type at 100%).
OP_PROPORTION = {
    "READ": "readproportion",
    "UPDATE": "updateproportion",
    "INSERT": "insertproportion",
    "SCAN": "scanproportion",
    "READ-MODIFY-WRITE": "readmodifywriteproportion",
}

WORKLOADS = {w: f"workloads/workload{w}" for w in ("a", "b", "c", "d", "e", "f")}


def cache_paths(workspace: Path) -> tuple[Path, Path]:
    cache = workspace / ".cache"
    home = cache / f"ycsb-redis-binding-{YCSB_VERSION}"
    return cache, home


def safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    members = tar.getmembers()
    for member in members:
        target = (destination / member.name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"unsafe YCSB archive path: {member.name}") from exc
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError(f"unsupported YCSB archive member: {member.name}")
    tar.extractall(destination, members=members)


def ensure_ycsb(*, cache: Path, home: Path, sha256: str = YCSB_SHA256, url: str = YCSB_URL) -> None:
    marker = home / ".vibeserve-sha256"
    if (home / "bin" / "ycsb.sh").is_file() and marker.is_file():
        if marker.read_text().strip() == sha256:
            return

    cache.mkdir(parents=True, exist_ok=True)
    lock_path = cache / f".ycsb-{YCSB_VERSION}.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (home / "bin" / "ycsb.sh").is_file() and marker.is_file():
            if marker.read_text().strip() == sha256:
                return
        print(f"Downloading YCSB {YCSB_VERSION} Redis binding...", file=sys.stderr)
        with tempfile.TemporaryDirectory(dir=cache) as tmp_dir:
            tmp = Path(tmp_dir)
            tarball = tmp / "ycsb.tar.gz"
            digest = hashlib.sha256()
            with urllib.request.urlopen(url) as response, tarball.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != sha256:
                raise RuntimeError("YCSB archive checksum mismatch")
            extract_root = tmp / "extract"
            extract_root.mkdir()
            with tarfile.open(tarball, "r:gz") as tar:
                safe_extract(tar, extract_root)
            extracted = extract_root / f"ycsb-redis-binding-{YCSB_VERSION}"
            if not (extracted / "bin" / "ycsb.sh").is_file():
                raise RuntimeError("YCSB archive is missing bin/ycsb.sh")
            (extracted / ".vibeserve-sha256").write_text(f"{sha256}\n")
            staged = cache / f".{home.name}.staged"
            if staged.exists():
                shutil.rmtree(staged)
            shutil.move(str(extracted), staged)
            if home.exists():
                shutil.rmtree(home)
            staged.replace(home)


def ycsb_cmd(
    home: Path,
    phase: str,
    workload: str,
    port: int,
    num_keys: int,
    threads: int,
    *,
    duration: int | None = None,
    extra: tuple[str, ...] | list[str] = (),
    record: tuple[str, ...] | list[str] = (),
) -> list[str]:
    props = [
        "-p",
        "redis.host=127.0.0.1",
        "-p",
        f"redis.port={port}",
        "-p",
        f"recordcount={num_keys}",
        "-p",
        f"operationcount={_OP_CAP}",
        "-p",
        f"threadcount={threads}",
        "-p",
        "hdrhistogram.percentiles=50,95,99,99.9",
        *record,
    ]
    if duration is not None and phase == "run":
        props += ["-p", f"maxexecutiontime={duration}"]
    props += list(extra)
    return [
        str(home / "bin" / "ycsb.sh"),
        phase,
        "redis",
        "-s",
        "-P",
        str(home / workload),
        *props,
    ]


def run_ycsb(
    home: Path,
    phase: str,
    workload: str,
    port: int,
    num_keys: int,
    threads: int,
    *,
    duration: int | None = None,
    extra: tuple[str, ...] | list[str] = (),
    record: tuple[str, ...] | list[str] = (),
) -> str:
    """Run a single YCSB phase to completion; return stdout (exits on failure)."""
    result = subprocess.run(
        ycsb_cmd(
            home,
            phase,
            workload,
            port,
            num_keys,
            threads,
            duration=duration,
            extra=extra,
            record=record,
        ),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        print(f"YCSB {phase} failed:\n{result.stderr[-2000:]}")
        sys.exit(1)
    return result.stdout


def parse_metrics(output: str) -> dict[str, float]:
    """Turn YCSB's `[GROUP], metric, value` CSV lines into {'GROUP.metric': value}."""
    metrics: dict[str, float] = {}
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[0].startswith("["):
            try:
                metrics[f"{parts[0].strip('[]')}.{parts[1]}"] = float(parts[2])
            except ValueError:
                pass
    return metrics


def pct_key(op: str, pct: str) -> str:
    # YCSB emits "50thPercentileLatency(us)" but "99.9PercentileLatency(us)" (no 'th').
    suffix = "th" if "." not in pct else ""
    return f"{op}.{pct}{suffix}PercentileLatency(us)"
