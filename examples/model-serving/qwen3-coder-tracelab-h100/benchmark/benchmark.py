"""Thin benchmark shim for the hidden TraceLab replay evaluator."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the trusted TraceLab replay benchmark. The TraceLab runner and "
            "released trace data are hidden from candidate agents and injected "
            "only for framework-owned benchmark execution."
        )
    )
    parser.add_argument("--url", default=os.environ.get("VIBESYS_SERVICE_URL"))
    parser.add_argument("--model", default="Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8")
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--max-sessions", type=int, default=16)
    parser.add_argument("--max-active-sessions", type=int, default=8)
    parser.add_argument("--arrival-rate", type=float, default=2.0)
    parser.add_argument("--provider", choices=["claude", "codex", "all"], default="all")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--max-model-len", type=int, default=262144)
    parser.add_argument("--token-pool-limit", type=int, default=1_000_000)
    parser.add_argument("--stream-idle-timeout-secs", type=int, default=7200)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _discover_modal_url() -> str | None:
    """Deploy the candidate Modal app and parse its public web endpoint URL."""
    if not os.environ.get("VIBESYS_MODAL_APP_NAME") or not Path("main.py").is_file():
        return None

    result = subprocess.run(
        ["modal", "deploy", "main.py"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(result.stdout, file=sys.stderr, end="")
    if result.returncode != 0:
        raise SystemExit(result.returncode)

    compact = re.sub(r"\s+", "", result.stdout)
    urls = re.findall(r"https://[^\"]+?\.modal\.run", compact)
    return urls[0] if urls else None


def _wait_for_health(base_url: str, timeout_secs: float = 900.0) -> None:
    health_url = base_url.rstrip("/") + "/health"
    if base_url.startswith("https://") and base_url.rstrip("/").endswith(".modal.run"):
        try:
            with urllib.request.urlopen(health_url, timeout=timeout_secs) as response:
                if response.status == 200:
                    print(f"Health check passed at {health_url}", file=sys.stderr)
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        raise SystemExit(f"Timed out waiting for Modal service health at {health_url}: {last_error}")

    deadline = time.monotonic() + timeout_secs
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=60.0) as response:
                if response.status == 200:
                    print(f"Health check passed at {health_url}", file=sys.stderr)
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(5.0)
    raise SystemExit(f"Timed out waiting for Modal service health at {health_url}: {last_error}")


def main() -> int:
    args = parse_args()
    hidden_dir = os.environ.get("VIBESYS_TRACELAB_EVALUATOR_DIR") or os.environ.get(
        "VIBESYS_HIDDEN_EVALUATOR_DIR"
    )
    if not hidden_dir:
        raise SystemExit(
            "VIBESYS_HIDDEN_EVALUATOR_DIR is not set. This benchmark must run through "
            "the VibeSys framework gate so TraceLab code and data remain hidden from agents."
        )

    runner = Path(hidden_dir) / "run_tracelab_replay.py"
    if not runner.is_file():
        raise SystemExit(f"hidden TraceLab replay runner not found: {runner}")

    url = args.url
    if url is None and not args.dry_run:
        url = _discover_modal_url()
    url = url or "http://127.0.0.1:8000"
    os.environ["VIBESYS_SERVICE_URL"] = url
    if not args.dry_run and url.startswith("https://") and url.endswith(".modal.run"):
        _wait_for_health(url)

    command = [
        sys.executable,
        str(runner),
        "--url",
        url,
        "--model",
        args.model,
        "--max-sessions",
        str(args.max_sessions),
        "--max-active-sessions",
        str(args.max_active_sessions),
        "--arrival-rate",
        str(args.arrival_rate),
        "--provider",
        args.provider,
        "--seed",
        str(args.seed),
        "--max-model-len",
        str(args.max_model_len),
        "--token-pool-limit",
        str(args.token_pool_limit),
        "--stream-idle-timeout-secs",
        str(args.stream_idle_timeout_secs),
    ]
    if args.output_json:
        command.extend(["--output-json", args.output_json])
    if args.dry_run:
        command.append("--dry-run")

    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
