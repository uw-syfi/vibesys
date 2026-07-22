"""Prepare real TraceLab data and invoke TraceLab's replay runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

TRACE_RELEASE_URL = (
    "https://github.com/uw-syfi/TraceLab/releases/download/v0.0.1/"
    "syfi_coding_trace.duckdb"
)
TRACE_DUCKDB_SHA256 = "97715265367cc72376475f5d444c8e1900b88cab1482aa7b9a742894d9f15619"
DEFAULT_MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"
MIN_CARGO_VERSION = (1, 85)
TOKENIZERS_CRATE_VERSION = "0.22"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output-json")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--max-sessions", type=int, default=16)
    parser.add_argument("--max-active-sessions", type=int, default=8)
    parser.add_argument("--arrival-rate", type=float, default=2.0)
    parser.add_argument("--provider", choices=["claude", "codex", "all"], default="all")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--max-model-len", type=int, default=262144)
    parser.add_argument("--token-pool-limit", type=int, default=1_000_000)
    parser.add_argument("--stream-idle-timeout-secs", type=int, default=7200)
    parser.add_argument("--tokenizer", type=Path, default=None)
    parser.add_argument("--text-file", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as out:
        shutil.copyfileobj(response, out)
    tmp.replace(destination)


def ensure_trace_db(cache_dir: Path) -> Path:
    db = cache_dir / "syfi_coding_trace.duckdb"
    if not db.exists():
        print(f"[tracelab] downloading pinned TraceLab DuckDB: {TRACE_RELEASE_URL}", file=sys.stderr)
        download(TRACE_RELEASE_URL, db)
    actual = sha256(db)
    if actual != TRACE_DUCKDB_SHA256:
        db.unlink(missing_ok=True)
        raise SystemExit(
            "TraceLab DuckDB checksum mismatch: "
            f"expected {TRACE_DUCKDB_SHA256}, got {actual}"
        )
    return db


def run(command: list[str], *, cwd: Path | None = None) -> None:
    printable = " ".join(command)
    print(f"[tracelab] {printable}", file=sys.stderr)
    subprocess.run(command, cwd=cwd, check=True)


def cargo_version() -> tuple[int, int] | None:
    try:
        output = subprocess.check_output(["cargo", "--version"], text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parts = output.split()
    if len(parts) < 2:
        return None
    version = parts[1].split(".")
    try:
        major = int(version[0])
        minor = int(version[1])
    except (IndexError, ValueError):
        return None
    return major, minor


def cargo_supports_lockfile_v4() -> bool:
    version = cargo_version()
    return version is not None and version >= (1, 78)


def ensure_modern_cargo(cache_dir: Path) -> None:
    version = cargo_version()
    if version is not None and version >= MIN_CARGO_VERSION:
        return

    rustup_home = cache_dir / "rustup"
    cargo_home = cache_dir / "cargo"
    cargo_bin = cargo_home / "bin" / "cargo"
    if not cargo_bin.is_file():
        installer = cache_dir / "rustup-init"
        if not installer.is_file():
            print("[tracelab] installing cached Rust toolchain for session_runner", file=sys.stderr)
            download("https://sh.rustup.rs", installer)
            installer.chmod(installer.stat().st_mode | stat.S_IXUSR)
        env = {
            **os.environ,
            "RUSTUP_HOME": str(rustup_home),
            "CARGO_HOME": str(cargo_home),
        }
        subprocess.run(
            [
                str(installer),
                "-y",
                "--profile",
                "minimal",
                "--default-toolchain",
                "stable",
                "--no-modify-path",
            ],
            check=True,
            env=env,
        )

    os.environ["RUSTUP_HOME"] = str(rustup_home)
    os.environ["CARGO_HOME"] = str(cargo_home)
    os.environ["PATH"] = f"{cargo_home / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    version = cargo_version()
    if version is None or version < MIN_CARGO_VERSION:
        raise SystemExit(
            "TraceLab session_runner requires Cargo "
            f">={MIN_CARGO_VERSION[0]}.{MIN_CARGO_VERSION[1]}; found {version}"
        )


def replay_manifest(tracelab_root: Path, cache_dir: Path) -> Path:
    """Return a replay Cargo manifest compatible with current Qwen tokenizers.

    TraceLab's replay runner is invoked directly, but its pinned
    ``tokenizers = 0.19`` dependency cannot parse Qwen3's current tokenizer JSON.
    Build a cached copy of the runner with a newer tokenizers crate instead of
    mutating the TraceLab submodule or exposing the wrapper to candidate agents.
    """
    compat = cache_dir / f"tracelab-replay-cargo-tokenizers-{TOKENIZERS_CRATE_VERSION}"
    manifest = compat / "Cargo.toml"
    if manifest.is_file() and f'tokenizers = "{TOKENIZERS_CRATE_VERSION}"' in manifest.read_text(
        encoding="utf-8"
    ):
        return manifest
    if compat.exists():
        shutil.rmtree(compat)
    shutil.copytree(
        tracelab_root / "replay",
        compat,
        ignore=lambda _d, names: ["Cargo.lock"] if "Cargo.lock" in names else [],
    )
    text = manifest.read_text(encoding="utf-8")
    text = text.replace('tokenizers = "0.19"', f'tokenizers = "{TOKENIZERS_CRATE_VERSION}"')
    manifest.write_text(text, encoding="utf-8")
    return manifest


def ensure_tokenizer(cache_dir: Path, model: str, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    tokenizer = cache_dir / "tokenizer" / model.replace("/", "--") / "tokenizer.json"
    if tokenizer.is_file():
        return tokenizer
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise SystemExit("huggingface_hub is required to fetch tokenizer.json") from exc
    snapshot = Path(
        snapshot_download(
            repo_id=model,
            allow_patterns=["tokenizer.json", "tokenizer_config.json", "*.model", "*.tiktoken"],
            local_dir=tokenizer.parent,
            local_dir_use_symlinks=False,
        )
    )
    found = snapshot / "tokenizer.json"
    if not found.is_file():
        raise SystemExit(f"tokenizer.json was not found for {model} in {snapshot}")
    return found


def ensure_text_corpus(cache_dir: Path, tracelab_root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    corpus = cache_dir / "text_corpus.txt"
    if corpus.is_file() and corpus.stat().st_size >= 32 * 1024 * 1024:
        return corpus
    sources = [
        tracelab_root / "README.md",
        tracelab_root / "replay" / "README.md",
        tracelab_root / "replay" / "src" / "main.rs",
        tracelab_root / "artifacts" / "trace_facts" / "csv_export" / "convert.py",
    ]
    chunks = [path.read_text(errors="ignore") for path in sources if path.is_file()]
    seed_text = "\n\n".join(chunks) or "def coding_agent_trace_replay():\n    return 'tokens'\n"
    corpus.parent.mkdir(parents=True, exist_ok=True)
    with corpus.open("w", encoding="utf-8") as fh:
        while fh.tell() < 64 * 1024 * 1024:
            fh.write(seed_text)
            fh.write("\n")
    return corpus


def normalize_base_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "http://" + url
    return url.rstrip("/").removesuffix("/v1") + "/v1"


def convert_trace(
    *,
    tracelab_root: Path,
    db: Path,
    csv_path: Path,
    args: argparse.Namespace,
) -> None:
    converter = tracelab_root / "artifacts" / "trace_facts" / "csv_export" / "convert.py"
    run(
        [
            sys.executable,
            str(converter),
            "--db",
            str(db),
            "-o",
            str(csv_path),
            "--arrival-rate",
            str(args.arrival_rate),
            "--provider",
            args.provider,
            "--max-sessions",
            str(args.max_sessions),
            "--seed",
            str(args.seed),
            "--session-order",
            "shuffle",
        ],
        cwd=tracelab_root,
    )


def replay(
    *,
    tracelab_root: Path,
    manifest_path: Path,
    csv_path: Path,
    tokenizer: Path,
    text_file: Path,
    summary_path: Path,
    log_path: Path,
    args: argparse.Namespace,
) -> float:
    command = [
        "cargo",
        "run",
        "--release",
        "--manifest-path",
        str(manifest_path),
        "--bin",
        "session_runner",
        "--",
        "--trace",
        str(csv_path),
        "--text-file",
        str(text_file),
        "--tokenizer",
        str(tokenizer),
        "--model",
        args.model,
        "--base-url",
        normalize_base_url(args.url),
        "--stream-idle-timeout-secs",
        str(args.stream_idle_timeout_secs),
        "--max-model-len",
        str(args.max_model_len),
        "--fail-on-context-overflow",
        "--max-active-sessions",
        str(args.max_active_sessions),
        "--token-pool-limit",
        str(args.token_pool_limit),
        "--summary-path",
        str(summary_path),
        "--log-path",
        str(log_path),
    ]
    if args.dry_run:
        command.append("--dry-run")
    start = time.perf_counter()
    run(command, cwd=tracelab_root)
    return time.perf_counter() - start


def emit_result(summary_path: Path, output_json: str | None, wall_time_s: float) -> None:
    summary = json.loads(summary_path.read_text())
    replay_summary = summary.get("replay") or {}
    workload = summary.get("workload") or {}
    output_tokens = float(replay_summary.get("actual_output_tokens") or 0.0)
    token_throughput = output_tokens / wall_time_s if wall_time_s > 0 else 0.0
    result = {
        "aggregate_output_tokens_per_second": token_throughput,
        "token_throughput": token_throughput,
        "wall_time_s": wall_time_s,
        "success_steps": replay_summary.get("success_steps", 0),
        "failed_steps": replay_summary.get("failed_steps", 0),
        "actual_output_tokens": replay_summary.get("actual_output_tokens", 0),
        "target_output_tokens": replay_summary.get("target_output_tokens", 0),
        "server_prefix_hit_rate": replay_summary.get("server_prefix_hit_rate"),
        "planned_prefix_hit_rate": replay_summary.get("planned_prefix_hit_rate"),
        "ttft_ms_p50": replay_summary.get("ttft_ms_p50"),
        "ttft_ms_p90": replay_summary.get("ttft_ms_p90"),
        "workload": workload,
        "trace_source": TRACE_RELEASE_URL,
        "runner": "TraceLab replay/session_runner",
    }
    encoded = json.dumps(result, indent=2, sort_keys=True)
    print(encoded)
    if output_json:
        Path(output_json).write_text(encoded + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    here = Path(__file__).resolve().parent
    tracelab_root = here / "tracelab"
    if not (tracelab_root / "replay" / "Cargo.toml").is_file():
        raise SystemExit(f"TraceLab submodule is missing or incomplete: {tracelab_root}")
    default_cache = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    cache_dir = (args.cache_dir or default_cache / "vibesys-tracelab").resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    ensure_modern_cargo(cache_dir)

    db = ensure_trace_db(cache_dir)
    csv_path = cache_dir / (
        f"trace-provider={args.provider}-sessions={args.max_sessions}-"
        f"rate={args.arrival_rate}-seed={args.seed}.csv"
    )
    convert_trace(tracelab_root=tracelab_root, db=db, csv_path=csv_path, args=args)
    if args.dry_run:
        tokenizer = args.tokenizer or cache_dir / "dry-run-tokenizer.json"
        text_file = args.text_file or cache_dir / "dry-run-corpus.txt"
        tokenizer.touch()
        text_file.touch()
    else:
        tokenizer = ensure_tokenizer(cache_dir, args.model, args.tokenizer)
        text_file = ensure_text_corpus(cache_dir, tracelab_root, args.text_file)
    summary_path = cache_dir / "session_runner_summary.json"
    log_path = cache_dir / "session_runner.jsonl"
    manifest_path = replay_manifest(tracelab_root, cache_dir)
    wall_time_s = replay(
        tracelab_root=tracelab_root,
        manifest_path=manifest_path,
        csv_path=csv_path,
        tokenizer=tokenizer,
        text_file=text_file,
        summary_path=summary_path,
        log_path=log_path,
        args=args,
    )
    emit_result(summary_path, args.output_json, wall_time_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
