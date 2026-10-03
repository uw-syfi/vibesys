"""Tiny-model CPU check of a candidate engine's lifecycle, in seconds and without a GPU.

Run from the candidate root (the directory holding `engine/`, `reference/`,
and `accuracy_checker/`):

    cpu_check/run.sh   # keeps its CPU-torch environment out of the candidate directory

It writes a randomly initialized tiny checkpoint of the Qwen3.5 architecture
(`tiny_model.py`), starts the candidate's server on it on the CPU
(`python -m engine.server --model <dir> --device cpu ...`, or the reference
server when `engine/server.py` does not exist), and replays two interleaved
three-round sessions whose prompts grow from round to round (`sessions.py`).
Each round must succeed, report `cached_tokens` within `[0, prompt tokens]`,
and produce the in-process reference engine's greedy tokens, except for a
departure at a near-tie. One streamed request must match its non-streamed
tokens and usage. `--expect-cache-hits` also fails a run in which no round
reports `cached_tokens > 0`, and replays the benchmark's prefix-cache preflight
(`preflight.py`): one 8192-token prompt sent twice in a row, streamed, with
`max_tokens` 1, whose second response must report `cached_tokens > 0`.

`--concurrency N` drives the candidate's batching scheduler instead of one
request at a time: N chained sessions, one session whose first prompt is
4443 tokens long, and one 1372-token teacher-forced scoring request (the
accuracy checker's `echo` + `logprobs` path) are all in flight together, each
session sending its rounds in order as the benchmark does. Set N above the
engine's admission cap so admission, queueing, and batched decode all run.

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import torch
from accuracy_checker.targets import Completion, ForcedStep, HttpTarget
from reference.engine import Engine, SamplingParams

from cpu_check import preflight, scoring, sessions
from cpu_check.tiny_model import VOCAB_SIZE, write_checkpoint

MODEL_NAME = "tiny-qwen3.5"
SERVER_START_SECONDS = 120.0
REQUEST_SECONDS = 60.0
STOP_SECONDS = 10.0
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

    def top2(self, tokens: list[int]) -> list[tuple[int, float]]:
        return [(s.top[0][0], s.top[0][1] - s.top[1][1]) for s in self.engine.score(tokens, 2)]


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

    def teacher_forced(self, prompt: list[int], cont: list[int]) -> list[ForcedStep]:
        target = HttpTarget(self.base_url, MODEL_NAME, REQUEST_SECONDS)
        try:
            return target.teacher_forced(prompt, cont)
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
    with log_path.open("w") as log:  # the child keeps its own descriptor
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


def check(
    root: Path,
    work_dir: Path,
    *,
    expect_cache_hits: bool = False,
    concurrency: int = 0,
    log=print,
) -> int:
    t0 = time.monotonic()
    model_dir = work_dir / "tiny-model"
    write_checkpoint(model_dir)
    reference = ReferenceAnswers(model_dir)
    names = sessions.concurrent_sessions(concurrency) if concurrency else sessions.SESSIONS
    rounds = sessions.plan(reference, VOCAB_SIZE, names=names)
    scored = scoring.plan(reference, VOCAB_SIZE) if concurrency else None
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
        connection = _FreshConnection(base_url)
        score_error = None
        if scored is None:
            outcomes = sessions.run(connection, rounds)
        else:
            long_prompt = sessions.SHARED_PREFIX + sessions.LONG_FIRST_INPUT
            log(
                f"concurrent: {len(names)} sessions (one with a {long_prompt}-token first "
                f"prompt) and a {len(scored.tokens)}-token scoring request at once"
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                score_future = pool.submit(scoring.check, connection, scored)
                outcomes = sessions.run_concurrent(connection, rounds)
                score_error = score_future.result()
        preflight_error = None
        if expect_cache_hits:
            with httpx.Client(timeout=REQUEST_SECONDS) as client:
                preflight_error = preflight.replay(client, base_url, MODEL_NAME, VOCAB_SIZE)
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
        try:
            proc.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    for o in outcomes:
        log(f"  [{'ok' if o.ok else 'FAIL'}] {o.describe()}")
    if scored is not None:
        log(
            f"  [{'ok' if score_error is None else 'FAIL'}] teacher-forced scoring of "
            f"{len(scored.tokens)} tokens: {score_error or 'top-1 matches the reference'}"
        )
    log(
        f"  [{'ok' if stream_error is None else 'FAIL'}] stream of {last.round.label}: "
        f"{stream_error or 'tokens and usage match the non-streamed response'}"
    )
    hits = sum(1 for o in outcomes if o.cached_tokens)
    log(f"cache-hit rounds {hits}/{len(outcomes)}")
    if not hits:
        log(
            f"  [{'FAIL' if expect_cache_hits else 'note'}] no cache hits in the chained "
            "rounds (cached_tokens > 0 is needed)"
        )
    if expect_cache_hits:
        log(
            f"  [{'ok' if preflight_error is None else 'FAIL'}] preflight replay: "
            f"{preflight_error or 'the second response reported cached_tokens > 0'}"
        )
    passed = (
        all(o.ok for o in outcomes)
        and stream_error is None
        and (hits > 0 or not expect_cache_hits)
        and preflight_error is None
        and score_error is None
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
        help="fail unless a chained round reports cached_tokens > 0 and the preflight replay hits",
    )
    p.add_argument(
        "--concurrency",
        type=_nonnegative,
        default=0,
        metavar="N",
        help="drive the batching scheduler: N sessions, a long-prompt session, and a scoring "
        "request at once; set N above the engine's admission cap (default 0: one at a time)",
    )
    args = p.parse_args()
    torch.set_num_threads(min(8, torch.get_num_threads()))
    options = {"expect_cache_hits": args.expect_cache_hits, "concurrency": args.concurrency}
    if args.work_dir is not None:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        sys.exit(check(args.root.resolve(), args.work_dir.resolve(), **options))
    # The in-process reference engine maps the tiny checkpoint until it is collected; on an
    # NFS /tmp, deleting a mapped file leaves a `.nfs*` placeholder that blocks the rmdir.
    with tempfile.TemporaryDirectory(prefix="cpu-check-", ignore_cleanup_errors=True) as tmp:
        sys.exit(check(args.root.resolve(), Path(tmp), **options))


def _nonnegative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


if __name__ == "__main__":
    main()
