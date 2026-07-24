from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

MODAL_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+\.modal\.run")
_backend_ready: asyncio.Event | None = None
_backend_url: str | None = None
_modal_process: subprocess.Popen[str] | None = None


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def _extract_modal_url(text: str) -> str | None:
    match = MODAL_URL_RE.search(re.sub(r"\s+", "", _strip_ansi(text)))
    if match is None:
        return None
    return match.group(0).rstrip(").,")


def _capture_modal_output(process: subprocess.Popen[str], loop: asyncio.AbstractEventLoop) -> None:
    global _backend_url
    assert process.stdout is not None
    recent_output = ""
    for raw_line in process.stdout:
        line = _strip_ansi(raw_line)
        recent_output = (recent_output + line)[-4000:]
        url = _extract_modal_url(line) or _extract_modal_url(recent_output)
        if url is not None and _backend_url is None:
            _backend_url = url
            if _backend_ready is not None:
                loop.call_soon_threadsafe(_backend_ready.set)
        print(raw_line, end="", file=sys.stderr, flush=True)


async def _ensure_backend() -> str:
    if _backend_url is not None:
        return _backend_url
    if _backend_ready is None:
        raise RuntimeError("Backend startup was not initialized")
    await asyncio.wait_for(_backend_ready.wait(), timeout=300)
    assert _backend_url is not None
    return _backend_url


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _backend_ready, _backend_url, _modal_process
    loop = asyncio.get_running_loop()
    _backend_ready = asyncio.Event()

    configured_url = os.environ.get("MODAL_BACKEND_URL")
    if configured_url:
        _backend_url = configured_url.rstrip("/")
        _backend_ready.set()
        yield
        return

    _modal_process = subprocess.Popen(
        ["modal", "serve", "main.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    threading.Thread(
        target=_capture_modal_output,
        args=(_modal_process, loop),
        daemon=True,
    ).start()

    try:
        yield
    finally:
        if _modal_process.poll() is None:
            _modal_process.send_signal(signal.SIGINT)
            try:
                _modal_process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _modal_process.terminate()


app = FastAPI(lifespan=lifespan)


async def _proxy_get(path: str) -> Response:
    backend = await _ensure_backend()
    async with httpx.AsyncClient(timeout=900.0) as client:
        response = await client.get(f"{backend}{path}")
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
    )


async def _proxy_post(path: str, request: Request) -> Response:
    backend = await _ensure_backend()
    body = await request.body()
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    async with httpx.AsyncClient(timeout=900.0) as client:
        response = await client.post(f"{backend}{path}", content=body, headers=headers)
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
    )


async def _proxy_stream(path: str, request: Request) -> StreamingResponse:
    backend = await _ensure_backend()
    body = await request.body()
    headers = {"content-type": request.headers.get("content-type", "application/json")}

    async def events() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{backend}{path}",
                content=body,
                headers=headers,
            ) as response:
                async for chunk in response.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _is_streaming(request: Request) -> bool:
    try:
        payload = await request.json()
    except Exception:
        return False
    return bool(payload.get("stream"))


@app.get("/health")
async def health() -> Response:
    return await _proxy_get("/health")


@app.get("/v1/models")
async def models() -> Response:
    return await _proxy_get("/v1/models")


@app.post("/v1/completions")
async def completions(request: Request) -> Response:
    if await _is_streaming(request):
        return await _proxy_stream("/v1/completions", request)
    return await _proxy_post("/v1/completions", request)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    if await _is_streaming(request):
        return await _proxy_stream("/v1/chat/completions", request)
    return await _proxy_post("/v1/chat/completions", request)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Modal-backed API bridge.")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", default=os.environ.get("PORT", "8000"))
    parser.add_argument("--model-path", default=os.environ.get("MODEL_PATH", "/model"))
    parser.add_argument("--served-model-name", default=os.environ.get("SERVED_MODEL_NAME", "llama"))
    parser.add_argument("--dtype", default=os.environ.get("DTYPE", "bfloat16"))
    parser.add_argument(
        "--gpu-memory-utilization",
        default=os.environ.get("GPU_MEMORY_UTILIZATION", "0.90"),
    )
    parser.add_argument("--max-model-len", default=os.environ.get("MAX_MODEL_LEN", "8192"))
    parser.add_argument(
        "--tensor-parallel-size",
        default=os.environ.get("TENSOR_PARALLEL_SIZE", "1"),
    )
    parser.add_argument("--vllm-arg", action="append", default=[])
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=int(args.port), log_level="info")


if __name__ == "__main__":
    main()
