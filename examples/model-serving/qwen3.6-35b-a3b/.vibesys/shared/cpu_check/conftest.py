"""Start the baseline engine on tiny random checkpoints, as ``python -m engine.server``.

Each server is a real process launched from the project root, the way every
task launches the candidate, and is reached only over HTTP. Startup waits are
bounded hang guards: a healthy server answers ``/health`` within seconds.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from tiny_checkpoint import set_eos, write_checkpoint

PROJECT_ROOT = Path(__file__).resolve().parents[3]
# The engine's own public functions are importable too, as `engine.<module>`.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CHUNK = 8  # small prefill chunks, so short prompts cross snapshot boundaries
_STARTUP_GUARD_SECONDS = 180


@dataclass(frozen=True)
class Server:
    url: str
    model: str
    checkpoint: Path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(checkpoint: Path, log: Path, *extra: str) -> Iterator[Server]:
    port = _free_port()
    with log.open("w") as sink:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "engine.server",
                "--model",
                str(checkpoint),
                "--port",
                str(port),
                "--prefill-chunk-tokens",
                str(CHUNK),
                *extra,
            ],
            cwd=PROJECT_ROOT,
            stdout=sink,
            stderr=subprocess.STDOUT,
            env={**os.environ, "HF_HUB_OFFLINE": "1"},
        )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + _STARTUP_GUARD_SECONDS
        while True:
            if process.poll() is not None:
                pytest.fail(f"engine exited with {process.returncode}:\n{log.read_text()}")
            try:
                if httpx.get(f"{url}/health", timeout=5).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"engine did not become healthy:\n{log.read_text()}")
            time.sleep(0.2)
        yield Server(url=url, model=str(checkpoint), checkpoint=checkpoint)
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)


@pytest.fixture(scope="session")
def checkpoint(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("tiny-qwen36")
    write_checkpoint(path)
    return path


@pytest.fixture(scope="session")
def server(checkpoint: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    yield from _serve(checkpoint, tmp_path_factory.mktemp("logs") / "server.log")


@pytest.fixture(scope="session")
def eos_token(server: Server) -> tuple[list[int], int, int]:
    """A greedy output, a token in it, and where that token first appears."""
    body = {
        "prompt": list(range(5, 40)),
        "max_tokens": 24,
        "temperature": 0,
        "ignore_eos": True,
        "return_token_ids": True,
    }
    ids = httpx.post(f"{server.url}/v1/completions", json=body, timeout=120).json()["choices"][0][
        "token_ids"
    ]
    token = next(t for t in ids[3:] if t not in ids[:3])
    return ids, token, ids.index(token)


@pytest.fixture(scope="session")
def cold_server(
    checkpoint: Path,
    eos_token: tuple[list[int], int, int],
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Server]:
    """The same weights with the prefix cache off and a chosen token declared end-of-sequence."""
    copy = tmp_path_factory.mktemp("tiny-qwen36-eos")
    for item in checkpoint.iterdir():
        (copy / item.name).write_bytes(item.read_bytes())
    set_eos(copy, [eos_token[1]])
    yield from _serve(
        copy, tmp_path_factory.mktemp("logs") / "cold.log", "--prefix-cache-entries", "0"
    )


@pytest.fixture(scope="session")
def rocm_experts_server(
    checkpoint: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Server]:
    """The same weights with the MoE experts implementation the engine uses on ROCm."""
    yield from _serve(
        checkpoint,
        tmp_path_factory.mktemp("logs") / "rocm-experts.log",
        "--experts-implementation",
        "baseline_matmul",
    )
