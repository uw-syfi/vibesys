"""Tiny-model CPU check of a candidate engine's lifecycle, in seconds and without a GPU.

Run from the candidate root (the directory holding `engine/`, `reference/`,
and `accuracy_checker/`):

    uv run --project cpu_check python -m cpu_check

It writes a randomly initialized tiny checkpoint of the Qwen3.5 architecture
(`tiny_model.py`), starts the candidate's server on it on the CPU
(`python -m engine.server --model <dir> --device cpu ...`, or the reference
server when `engine/server.py` does not exist), and replays two interleaved
three-round sessions whose prompts grow from round to round (`sessions.py`).
Each round must succeed, report `cached_tokens` within `[0, prompt tokens]`,
and produce the in-process reference engine's greedy tokens, except for a
departure at a near-tie. One streamed request must match its non-streamed
tokens and usage. `--expect-cache-hits` also fails a run in which no round
reports `cached_tokens > 0`, which the benchmark's prefix-cache preflight needs.

Exit 0: pass. Exit 1: a round failed; each failure names its session and
round, followed by the server log tail. Exit 2: the server did not start.

This is an early signal, not the accuracy gate: the tiny model shares the
architecture and the engine code paths, not the 9B weights or numerics. The
trusted evaluation's accuracy checker stays the gate.
"""

from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import httpx
import torch
from accuracy_checker.targets import Completion, HttpTarget
from reference.engine import Engine, SamplingParams

from cpu_check import sessions
from cpu_check.tiny_model import VOCAB_SIZE, write_checkpoint

MODEL_NAME = "tiny-qwen3.5"
SERVER_START_SECONDS = 120.0
REQUEST_SECONDS = 60.0
LOG_TAIL_LINES = 40
_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception): ")


class ReferenceAnswers:
    """Greedy tokens and per-position margins from the in-process reference engine."""

    def __init__(self, model_dir: Path) -> None:
        self.engine = Engine(str(model_dir), device="cpu")

    def greedy(self, prompt: list[int], n: int) -> list[int]:
        params = SamplingParams(max_tokens=n, temperature=0.0, ignore_eos=True)
        return [s.token_id for s in self.engine.generate(prompt, params)]

    def margins(self, prompt: list[int], cont: list[int]) -> list[float]:
        scored = self.engine.score(prompt + cont, top_k=2)[len(prompt) - 1 :]
        return [s.top[0][1] - s.top[1][1] for s in scored]


class _FreshConnection:
    """One connection per request: a server that drops its connection after a failed
    request must not turn every later round into a connection error."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def complete(self, prompt: list[int], n: int) -> Completion:
        target = HttpTarget(self.base_url, MODEL_NAME, REQUEST_SECONDS)
        try:
            return target.complete(prompt, n)
        finally:
            target.client.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server_module(root: Path) -> str:
    return "engine.server" if (root / "engine" / "server.py").is_file() else "reference.server"


def _start_server(root: Path, model_dir: Path, port: int, log_path: Path) -> subprocess.Popen:
    argv = [
        sys.executable,
        "-m",
        _server_module(root),
        "--model",
        str(model_dir),
        "--served-model-name",
        MODEL_NAME,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--device",
        "cpu",
    ]
    log = log_path.open("w")
    return subprocess.Popen(argv, cwd=root, stdout=log, stderr=subprocess.STDOUT)


def _wait_healthy(proc: subprocess.Popen, base_url: str) -> str | None:
    """None once `/health` answers 200; otherwise why the server is not up."""
    deadline = time.monotonic() + SERVER_START_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return f"server exited with status {proc.returncode} before becoming healthy"
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                return None
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return f"server not healthy after {SERVER_START_SECONDS:.0f} s"


def _tail(log_path: Path) -> str:
    with log_path.open(errors="replace") as f:
        return "".join(deque(f, maxlen=LOG_TAIL_LINES))


def _first_exception(log_path: Path) -> str | None:
    """The first `SomethingError: message` line of the server log: usually the root cause."""
    with log_path.open(errors="replace") as f:
        return next((line.strip() for line in f if _EXCEPTION_LINE.match(line)), None)


def check(root: Path, work_dir: Path, *, expect_cache_hits: bool = False, log=print) -> int:
    t0 = time.monotonic()
    model_dir = work_dir / "tiny-model"
    write_checkpoint(model_dir)
    rounds = sessions.plan(ReferenceAnswers(model_dir), VOCAB_SIZE)
    log(f"reference answers for {len(rounds)} rounds in {time.monotonic() - t0:.1f} s")

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = work_dir / "server.log"
    log(f"starting {_server_module(root)} on the tiny model (log: {log_path})")
    proc = _start_server(root, model_dir, port, log_path)
    try:
        why = _wait_healthy(proc, base_url)
        if why is not None:
            log(f"FAIL: {why}\n--- server log tail ---\n{_tail(log_path)}")
            return 2
        outcomes = sessions.run(_FreshConnection(base_url), rounds)
        last = outcomes[-1]
        stream_error = "skipped: its non-streamed request failed"
        if last.error is None:
            try:
                stream_error = HttpTarget(base_url, MODEL_NAME, REQUEST_SECONDS).stream_matches(
                    last.round.prompt, len(last.token_ids), list(last.token_ids)
                )
            except Exception as exc:  # any failure is the finding to report
                stream_error = f"{type(exc).__name__}: {exc}"
    finally:
        proc.terminate()
        proc.wait()

    for o in outcomes:
        log(f"  [{'ok' if o.ok else 'FAIL'}] {o.describe()}")
    log(
        f"  [{'ok' if stream_error is None else 'FAIL'}] stream of {last.round.label}: "
        f"{stream_error or 'tokens and usage match the non-streamed response'}"
    )
    hits = sum(1 for o in outcomes if o.cached_tokens)
    log(f"cache-hit rounds {hits}/{len(outcomes)}")
    if not hits:
        log(
            f"  [{'FAIL' if expect_cache_hits else 'note'}] no cache hits; the benchmark's "
            "prefix-cache preflight needs cached_tokens > 0"
        )
    passed = (
        all(o.ok for o in outcomes) and stream_error is None and (hits > 0 or not expect_cache_hits)
    )
    log(f"{'PASS' if passed else 'FAIL'} in {time.monotonic() - t0:.1f} s")
    if not passed:
        first = _first_exception(log_path)
        if first is not None:
            log(f"first server exception: {first}")
        log(f"--- server log tail ---\n{_tail(log_path)}")
    return 0 if passed else 1


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--root", type=Path, default=Path.cwd(), help="candidate root (default: cwd)")
    p.add_argument("--work-dir", type=Path, help="keep the tiny model and server log here")
    p.add_argument(
        "--expect-cache-hits",
        action="store_true",
        help="fail unless some chained round reports cached_tokens > 0",
    )
    args = p.parse_args()
    torch.set_num_threads(min(8, torch.get_num_threads()))
    if args.work_dir is not None:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        work_dir = args.work_dir.resolve()
        sys.exit(check(args.root.resolve(), work_dir, expect_cache_hits=args.expect_cache_hits))
    with tempfile.TemporaryDirectory(prefix="cpu-check-") as tmp:
        sys.exit(check(args.root.resolve(), Path(tmp), expect_cache_hits=args.expect_cache_hits))


if __name__ == "__main__":
    main()
