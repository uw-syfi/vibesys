"""Check candidate parity against the pinned Q4_1 GGUF on a CPU reference.

Public API: ``parse_reference_step`` validates native llama.cpp probability
records. Candidate validation and error budgets reuse checker.py's public API.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import runpy
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

MODEL = "RecursiveSI/flint-int4-gguf"
REVISION = "9c47151036bc2c34331142dda04488bceab2bc9f"
FILENAME = "flint-int4-q4_1.gguf"
MODEL_SHA256 = "43c07baccdba42eb7528a9630ee15699b1942c297291dc1b39a061f508ee1a4b"
LLAMA_REVISION = "785322d17f2d51e5185cf996a1f4e46d87640841"
ARCHIVE_SHA256 = "c77fdd044067e47fbbd8bc1bfde0992e630e93ce7dd1e546cafddd80ed85d84a"
VOCAB_SIZE = 150144
TOP_K = 32


def parse_reference_step(payload: object, chosen: int) -> tuple[float, float]:
    """Return chosen/best logprobs, failing closed outside reference top-32."""
    if not isinstance(payload, dict):
        raise ValueError("CPU completion must be an object")
    rows = payload.get("completion_probabilities")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("CPU completion_probabilities must contain exactly one step")
    row = rows[0]
    top = row.get("top_logprobs")
    if not isinstance(top, list) or not 1 <= len(top) <= TOP_K:
        raise ValueError("CPU top_logprobs must contain 1..32 observations")
    probabilities = {}
    for item in [row, *top]:
        if not isinstance(item, dict):
            raise ValueError("CPU probability entry must be an object")
        token, value = item.get("id"), item.get("logprob")
        if type(token) is not int or not 0 <= token < VOCAB_SIZE:
            raise ValueError("CPU probability id must be in the vocabulary")
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value > 0
        ):
            raise ValueError("CPU logprob must be finite and nonpositive")
        if token in probabilities and probabilities[token] != value:
            raise ValueError("CPU probability id has inconsistent logprobs")
        probabilities[token] = float(value)
    if chosen not in probabilities:
        raise ValueError("candidate token is outside CPU reference top-32")
    best = max(probabilities.values())
    if probabilities[row["id"]] != best:
        raise ValueError("CPU greedy token is not the reference maximum")
    return probabilities[chosen], best


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _build(directory: Path) -> Path:
    archive = directory / "source.tar.gz"
    with (
        urllib.request.urlopen(
            f"https://codeload.github.com/ggml-org/llama.cpp/tar.gz/{LLAMA_REVISION}", timeout=120
        ) as response,
        archive.open("wb") as output,
    ):
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    if _digest(archive) != ARCHIVE_SHA256:
        raise ValueError("llama.cpp source archive SHA256 mismatch")
    with tarfile.open(archive) as source:
        source.extractall(directory, filter="data")
    root = directory / f"llama.cpp-{LLAMA_REVISION}"
    build = directory / "build"
    subprocess.run(
        [
            "cmake",
            "-S",
            str(root),
            "-B",
            str(build),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DGGML_CUDA=OFF",
            "-DGGML_HIP=OFF",
            "-DGGML_METAL=OFF",
            "-DGGML_VULKAN=OFF",
            "-DLLAMA_BUILD_TESTS=OFF",
            "-DLLAMA_BUILD_EXAMPLES=OFF",
            "-DLLAMA_BUILD_SERVER=ON",
            "-DLLAMA_BUILD_UI=OFF",
            "-DLLAMA_USE_PREBUILT_UI=OFF",
        ],
        check=True,
        timeout=180,
    )
    subprocess.run(
        ["cmake", "--build", str(build), "--target", "llama-server", "-j", "2"],
        check=True,
        timeout=1200,
    )
    return build / "bin/llama-server"


@contextmanager
def _reference(directory: Path):
    from huggingface_hub import hf_hub_download

    model = Path(hf_hub_download(MODEL, FILENAME, revision=REVISION))
    if _digest(model) != MODEL_SHA256:
        raise ValueError("Flint Q4_1 GGUF SHA256 mismatch")
    binary = _build(directory)
    endpoint = directory / "reference.sock"
    with (directory / "reference.log").open("w+") as log:
        # CPU-only build, explicit zero offload, and a private Unix socket keep
        # the reference independent of candidate GPU memory and listening ports.
        process = subprocess.Popen(
            [
                str(binary),
                "-m",
                str(model),
                "--host",
                str(endpoint),
                "-ngl",
                "0",
                "-c",
                "2048",
                "-np",
                "1",
                "-b",
                "128",
                "-ub",
                "128",
                "--jinja",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(endpoint)), timeout=120
            ) as client:
                deadline = time.monotonic() + 240
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(
                            "CPU reference exited during startup; inspect reference.log"
                        )
                    try:
                        response = client.get("http://reference/health")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("CPU reference startup exceeded 240 seconds")
                    time.sleep(1)
                yield client
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
            log.seek(0)
            sys.stderr.write(log.read()[-4000:])


def _post(client: httpx.Client, path: str, body: dict) -> Any:
    response = client.post("http://reference" + path, json=body)
    response.raise_for_status()
    return response.json()


def _score(reference: httpx.Client, prompt: list[int], ids: tuple[int, ...], shared: Any) -> list:
    scores = []
    for index, chosen in enumerate(ids):
        payload = _post(
            reference,
            "/completion",
            {
                "prompt": prompt + list(ids[:index]),
                "n_predict": 1,
                "temperature": -1,
                "n_probs": TOP_K,
                "post_sampling_probs": False,
                "ignore_eos": True,
                "cache_prompt": True,
            },
        )
        scores.append(shared.ReferenceStep(*parse_reference_step(payload, chosen)))
    return scores


def _check(candidate: httpx.Client, reference: httpx.Client, url: str) -> None:
    shared = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name("checker.py"))))
    pool = _post(
        reference,
        "/tokenize",
        {
            "content": f"# case {secrets.token_hex(16)}\ndef merge(xs):\n    return sorted(set(xs))\n"
            * 512,
            "add_special": False,
        },
    )["tokens"]
    for length in shared.PROMPT_LENGTHS:
        prompt = pool[:length]
        body = {
            "model": MODEL,
            "prompt": prompt,
            "max_tokens": shared.GENERATED_TOKENS,
            "temperature": 0,
            "ignore_eos": True,
            "return_token_ids": True,
            "logprobs": 1,
        }
        observed = []
        for _ in range(2):
            response = candidate.post(url + "/v1/completions", json=body)
            response.raise_for_status()
            observed.append(shared.parse_completion(response.json(), length, VOCAB_SIZE))
        if observed[0].ids != observed[1].ids:
            raise ValueError(f"candidate greedy IDs are not repeatable at length {length}")
        shared.assess(observed[0], _score(reference, prompt, observed[0].ids, shared))
        sys.stdout.write(f"PASS Q4_1: prompt={length}\n")
    messages = [{"role": "user", "content": "Return Python code that reverses xs."}]
    text = _post(reference, "/apply-template", {"messages": messages})["prompt"]
    prompt = _post(reference, "/tokenize", {"content": text, "add_special": False})["tokens"]
    body = {
        "model": MODEL,
        "max_tokens": shared.GENERATED_TOKENS,
        "temperature": 0,
        "ignore_eos": True,
        "return_token_ids": True,
    }
    raw_response = candidate.post(
        url + "/v1/completions", json={**body, "prompt": prompt, "logprobs": 1}
    )
    raw_response.raise_for_status()
    raw = shared.parse_completion(raw_response.json(), len(prompt), VOCAB_SIZE)
    response = candidate.post(
        url + "/v1/chat/completions", json={**body, "messages": messages, "logprobs": True}
    )
    response.raise_for_status()
    chat = shared.parse_completion(response.json(), len(prompt), VOCAB_SIZE, endpoint="chat")
    if raw.ids != chat.ids:
        raise ValueError("candidate chat differs from GGUF template completion")
    shared.assess(raw, _score(reference, prompt, raw.ids, shared))
    shared.assess(chat, _score(reference, prompt, chat.ids, shared))
    sys.stdout.write("PASS Q4_1: chat template parity\n")


def main() -> int:
    """Build an independent CPU reference and check the live candidate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="flint-q4-") as directory:
            with _reference(Path(directory)) as reference, httpx.Client(timeout=120) as candidate:
                _check(candidate, reference, args.url.rstrip("/").removesuffix("/v1"))
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
        httpx.HTTPError,
    ) as exc:
        sys.stderr.write(f"Flint Q4_1 accuracy failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
