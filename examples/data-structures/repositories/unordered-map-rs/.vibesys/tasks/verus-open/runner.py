from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TASK_ROOT.parents[2]
CANDIDATE_ROOT = PROJECT_ROOT / "verus-map"
TARGET_ROOT = Path(
    os.environ.get(
        "VIBESYS_VERUS_TASK_TARGET",
        str(PROJECT_ROOT / "target" / "verus-unordered-map-task"),
    )
)
METRIC_PREFIX = "total_ops_per_sec="
FORBIDDEN_SOURCE_PATTERNS = (
    (re.compile(r"\bassume\s*\("), "assume"),
    (re.compile(r"\badmit\s*\("), "admit"),
    (re.compile(r"\baxiom\b"), "axiom"),
    (re.compile(r"\bexternal_body\b"), "external_body"),
    (re.compile(r"\bexternal_fn_specification\b"), "external_fn_specification"),
    (re.compile(r"\bverifier\s*::\s*external\b"), "verifier::external"),
    (re.compile(r"\binclude(?:_bytes|_str)?\s*!"), "include macro"),
    (re.compile(r"#\s*\[\s*path\s*="), "path attribute"),
    (re.compile(r"#\s*\[\s*cfg(?:_attr)?\s*\("), "cfg attribute"),
    (re.compile(r"\bcfg\s*!\s*\("), "cfg macro"),
)
FIXED_CANDIDATE_FILES = {
    ".gitignore": "306fd52e74fca6746e12acc750f232de15e71da917756a1270d74c91f5eb7368",
    "Cargo.lock": "09dfbafcee0754ae7bf20c6ef275f9442d1c3804472846eff984ebe70cc1a9a5",
    "Cargo.toml": "1be1e7afc81a35b71eb19115eb3ebaafbcb6bae8bd0a4db9656d0e1dab487983",
    "README.md": "2eea446ae8f0b1879471896a0c1efb2b263e1562ccc76162b6129bbd1d389fd6",
    "src/lib.rs": "831b1f8b204339af3cdf7a6bbc8cdd6881e8be7d0a37c9f614ec2d8b7ae2eda1",
    "src/contract.rs": "0527c0d429d99d4a87e1686bd97e716fc07eb29f564c1f4b87d2edbd8f199582",
    "src/api.rs": "66f56fe16b74f3a498a9b44582416623b1d293cda397dd9829b70cafad02b52d",
}


def _run(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(TARGET_ROOT / "candidate")
    try:
        return subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=True,
            capture_output=capture_output,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"required command was not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        if capture_output:
            detail = (exc.stderr or exc.stdout or "command failed").strip()
            raise RuntimeError(detail) from exc
        raise RuntimeError(f"command failed with exit code {exc.returncode}") from exc


def _verify_candidate(candidate_root: Path = CANDIDATE_ROOT) -> None:
    candidate_manifest = candidate_root / "Cargo.toml"
    if not candidate_manifest.is_file():
        raise RuntimeError(f"candidate manifest not found: {candidate_manifest}")
    for relative_path, expected_digest in FIXED_CANDIDATE_FILES.items():
        path = candidate_root / relative_path
        if not path.is_file():
            raise RuntimeError(f"fixed candidate file is missing: {relative_path}")
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise RuntimeError(f"implementer modified fixed candidate file: {relative_path}")
    for path in candidate_root.rglob("*"):
        relative_path = path.relative_to(candidate_root)
        if path.is_symlink():
            raise RuntimeError(f"candidate source tree contains a symlink: {relative_path}")
        if relative_path.parts[0] == "target":
            continue
        if path.is_dir():
            if relative_path == Path("src") or Path("src/candidate") in (
                relative_path,
                *relative_path.parents,
            ):
                continue
            raise RuntimeError(f"unexpected directory outside src/candidate: {relative_path}")
        if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise RuntimeError(
                f"candidate source tree contains a non-regular file: {relative_path}"
            )
        if relative_path in (Path(name) for name in FIXED_CANDIDATE_FILES):
            continue
        if Path("src/candidate") not in relative_path.parents or path.suffix != ".rs":
            raise RuntimeError(f"unexpected file outside src/candidate: {relative_path}")
    manifest = tomllib.loads(candidate_manifest.read_text(encoding="utf-8"))
    if manifest.get("package", {}).get("metadata", {}).get("verus", {}).get("verify") is not True:
        raise RuntimeError("candidate must keep package.metadata.verus.verify = true")
    for source in (candidate_root / "src" / "candidate").rglob("*.rs"):
        contents = source.read_text(encoding="utf-8")
        forbidden = next(
            (
                description
                for pattern, description in FORBIDDEN_SOURCE_PATTERNS
                if pattern.search(contents)
            ),
            None,
        )
        if forbidden is not None:
            raise RuntimeError(f"forbidden source construct {forbidden!r} in {source}")
    _run(
        [
            "cargo",
            "check",
            "--manifest-path",
            str(candidate_manifest),
            "--locked",
        ]
    )
    if shutil.which("cargo-verus") is None:
        raise RuntimeError(
            "cargo-verus was not found on PATH; install the Verus release that matches vstd"
        )
    _run(
        [
            "cargo",
            "verus",
            "verify",
            "--manifest-path",
            str(candidate_manifest),
            "--locked",
            "--",
            "--num-threads",
            "1",
        ]
    )


def _run_task_crate(
    crate: str,
    arguments: list[str],
    *,
    release: bool = False,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    source_root = TASK_ROOT / crate
    with tempfile.TemporaryDirectory(prefix=f"{crate}-", dir=TARGET_ROOT) as temporary:
        staged_root = Path(temporary)
        (staged_root / "src").mkdir()
        manifest_template = (source_root / "Cargo.toml.in").read_text(encoding="utf-8")
        candidate_path = json.dumps(str(CANDIDATE_ROOT))
        (staged_root / "Cargo.toml").write_text(
            manifest_template.replace("__CANDIDATE_PATH__", candidate_path),
            encoding="utf-8",
        )
        shutil.copy2(source_root / "src" / "main.rs", staged_root / "src" / "main.rs")
        environment = os.environ.copy()
        environment["CARGO_TARGET_DIR"] = str(TARGET_ROOT / crate)
        cargo_arguments = ["cargo", "run", "--quiet"]
        if release:
            cargo_arguments.append("--release")
        cargo_arguments.extend(
            [
                "--manifest-path",
                str(staged_root / "Cargo.toml"),
                "--",
                *arguments,
            ]
        )
        try:
            return subprocess.run(
                cargo_arguments,
                cwd=PROJECT_ROOT,
                env=environment,
                check=True,
                capture_output=capture_output,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("required command was not found: cargo") from exc
        except subprocess.CalledProcessError as exc:
            if capture_output:
                detail = (exc.stderr or exc.stdout or "harness failed").strip()
                raise RuntimeError(detail) from exc
            raise RuntimeError(f"harness failed with exit code {exc.returncode}") from exc


def _check() -> None:
    _verify_candidate()
    _run_task_crate("accuracy", [])


def _check_fixture() -> None:
    fixture = TASK_ROOT / "acceptance" / "alternate-lp" / "src" / "candidate"
    if not fixture.is_dir():
        raise RuntimeError(f"acceptance fixture not found: {fixture}")
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="alternate-lp-", dir=TARGET_ROOT) as temporary:
        staged = Path(temporary) / "verus-map"
        shutil.copytree(CANDIDATE_ROOT, staged, ignore=shutil.ignore_patterns("target"))
        staged_candidate = staged / "src" / "candidate"
        shutil.rmtree(staged_candidate)
        shutil.copytree(fixture, staged_candidate)
        _verify_candidate(staged)


def _benchmark(args: argparse.Namespace) -> None:
    if args.duration_seconds <= 0:
        raise RuntimeError("--duration-seconds must be greater than zero")
    for name in ("clients", "key_space"):
        if getattr(args, name) <= 0:
            raise RuntimeError(f"--{name.replace('_', '-')} must be greater than zero")

    duration_ms = max(1, round(args.duration_seconds * 1000))
    completed = _run_task_crate(
        "benchmark",
        [
            str(duration_ms),
            str(args.clients),
            str(args.key_space),
        ],
        release=True,
        capture_output=True,
    )
    metric_line = next(
        (line for line in completed.stdout.splitlines() if line.startswith(METRIC_PREFIX)),
        None,
    )
    if metric_line is None:
        raise RuntimeError("benchmark harness did not report total_ops_per_sec")
    try:
        throughput = float(metric_line.removeprefix(METRIC_PREFIX))
    except ValueError as exc:
        raise RuntimeError("benchmark harness reported an invalid throughput") from exc

    result = {
        "total_ops_per_sec": throughput,
        "duration_seconds": args.duration_seconds,
        "clients": args.clients,
        "key_space": args.key_space,
    }
    if args.output_json is not None:
        output = Path(args.output_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pure-Rust Verus unordered-map task runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    subparsers.add_parser("check-fixture")
    benchmark = subparsers.add_parser("benchmark")
    benchmark.add_argument("--duration-seconds", type=float, default=1.0)
    benchmark.add_argument("--clients", type=int, default=4)
    benchmark.add_argument("--key-space", dest="key_space", type=int, default=256)
    benchmark.add_argument("--output-json", type=str, default=None)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    if args.command == "check":
        _check()
    elif args.command == "check-fixture":
        _check_fixture()
    else:
        _benchmark(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL - {exc}")
        raise SystemExit(1) from None
