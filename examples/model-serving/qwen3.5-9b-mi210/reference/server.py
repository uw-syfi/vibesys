"""OpenAI-compatible text-completions server over the reference `Engine`.

    python -m reference.server --model Qwen/Qwen3.5-9B --host 0.0.0.0 --port 8000

Response shapes follow vLLM's `/v1/completions` (the Request Factory
`session_runner --backend openai` client was validated against vLLM),
including the vLLM extensions `ignore_eos`, `min_tokens`, `return_token_ids`,
and `return_tokens_as_token_ids`.

Execution model: one GPU worker thread runs requests strictly FIFO, one at a
time. HTTP handlers only enqueue work, detokenize, and format responses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from .engine import Engine, SamplingParams, StepOutput, TokenLogprobs

log = logging.getLogger("reference.server")

# ----------------------------------------------------------------------------- request schema


class StreamOptions(BaseModel):
    include_usage: bool = False
    continuous_usage_stats: bool = False


class CompletionRequest(BaseModel):
    # Unknown keys are accepted and ignored, as vLLM does (session_runner sends `rid`).
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    prompt: str | list[int] | list[str] | list[list[int]]
    max_tokens: int | None = 16
    temperature: float | None = 1.0
    top_p: float | None = 1.0
    n: int = 1
    seed: int | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    logprobs: int | None = None
    echo: bool = False
    stop: str | list[str] | None = None
    ignore_eos: bool = False
    min_tokens: int = 0
    return_token_ids: bool = False
    return_tokens_as_token_ids: bool = False


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _error(message: str, status: int) -> JSONResponse:
    kind = "NotFoundError" if status == 404 else "BadRequestError"
    return JSONResponse(
        {"error": {"message": message, "type": kind, "param": None, "code": status}}, status
    )


# ----------------------------------------------------------------------------- worker


_DONE = object()


@dataclass
class Job:
    """Unit of GPU work. `run` executes on the worker thread and pushes results through `emit`."""

    run: Callable[[Callable[[Any], None], Callable[[], bool]], None]
    emit: Callable[[Any], None]
    cancelled: threading.Event = field(default_factory=threading.Event)


class Worker:
    """Single GPU thread; requests run FIFO, one at a time."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[Job] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="gpu-worker", daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        return self._thread.is_alive()

    def submit(self, job: Job) -> None:
        self._jobs.put(job)

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job.cancelled.is_set():
                job.emit(_DONE)
                continue
            try:
                job.run(job.emit, job.cancelled.is_set)
            except Exception as exc:  # surfaced to the HTTP handler
                log.exception("request failed")
                job.emit(exc)
            job.emit(_DONE)


async def _submit(worker: Worker, run: Callable) -> tuple[AsyncIterator[Any], threading.Event]:
    """Enqueue `run`; return an async iterator over what it emits, plus its cancel flag."""
    loop = asyncio.get_running_loop()
    items: asyncio.Queue[Any] = asyncio.Queue()
    job = Job(run=run, emit=lambda item: loop.call_soon_threadsafe(items.put_nowait, item))
    worker.submit(job)

    async def iterate() -> AsyncIterator[Any]:
        while True:
            item = await items.get()
            if item is _DONE:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    return iterate(), job.cancelled


# ----------------------------------------------------------------------------- formatting


class Detokenizer:
    """Incremental detokenization; holds back text while the tail is an incomplete UTF-8 sequence."""

    def __init__(self, tokenizer) -> None:
        self._tok = tokenizer
        self._ids: list[int] = []
        self._emitted = 0

    def push(self, token_id: int, final: bool) -> str:
        self._ids.append(token_id)
        text = self._tok.decode(self._ids, skip_special_tokens=True)
        if text.endswith("�") and not final:
            return ""
        delta = text[self._emitted :]
        self._emitted = len(text)
        return delta


@dataclass
class LogprobsBuilder:
    """OpenAI legacy-completions `logprobs` object."""

    tokenizer: Any
    as_token_ids: bool
    tokens: list[str] = field(default_factory=list)
    token_logprobs: list[float | None] = field(default_factory=list)
    top_logprobs: list[dict[str, float] | None] = field(default_factory=list)
    text_offset: list[int] = field(default_factory=list)
    _offset: int = 0

    def _name(self, token_id: int) -> str:
        return f"token_id:{token_id}" if self.as_token_ids else self.tokenizer.decode([token_id])

    def add(self, token_id: int, lp: TokenLogprobs | None) -> None:
        name = self._name(token_id)
        self.tokens.append(name)
        self.text_offset.append(self._offset)
        self._offset += len(name)
        if lp is None:  # first prompt token has no conditional distribution
            self.token_logprobs.append(None)
            self.top_logprobs.append(None)
            return
        self.token_logprobs.append(lp.logprob)
        top = {self._name(t): v for t, v in lp.top}
        top.setdefault(name, lp.logprob)
        self.top_logprobs.append(top)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text_offset": self.text_offset,
            "token_logprobs": self.token_logprobs,
            "tokens": self.tokens,
            "top_logprobs": self.top_logprobs,
        }


def _usage(prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "completion_tokens": completion_tokens,
        # No prefix caching in the reference engine.
        "prompt_tokens_details": {"cached_tokens": 0},
    }


# ----------------------------------------------------------------------------- app


def build_app(engine: Engine, served_model_name: str) -> FastAPI:
    app = FastAPI()
    worker = Worker()
    tok = engine.tokenizer

    def prompt_ids_of(req: CompletionRequest) -> list[int]:
        prompt = req.prompt
        if isinstance(prompt, list) and prompt and not isinstance(prompt[0], int):
            if len(prompt) != 1:
                raise BadRequest("batched prompts are not supported; send one prompt per request")
            prompt = prompt[0]
        ids = (
            tok(prompt, add_special_tokens=False).input_ids
            if isinstance(prompt, str)
            else list(prompt)
        )
        if not ids:
            raise BadRequest("prompt must contain at least one token")
        if any(not 0 <= t < engine.cfg.vocab_size for t in ids):
            raise BadRequest("prompt contains out-of-range token ids")
        return ids

    def params_of(req: CompletionRequest) -> SamplingParams:
        if req.model is not None and req.model != served_model_name:
            raise BadRequest(f"The model `{req.model}` does not exist.", status=404)
        if req.n != 1:
            raise BadRequest("only n=1 is supported")
        if req.stop:
            raise BadRequest("stop strings are not supported by the reference engine")
        if req.echo and req.stream:
            raise BadRequest("echo is not supported with stream=true")
        try:
            return SamplingParams(
                max_tokens=16 if req.max_tokens is None else req.max_tokens,
                temperature=1.0 if req.temperature is None else req.temperature,
                top_p=1.0 if req.top_p is None else req.top_p,
                seed=req.seed,
                ignore_eos=req.ignore_eos,
                min_tokens=req.min_tokens,
                logprobs=req.logprobs,
            )
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc

    @app.exception_handler(BadRequest)
    async def _bad_request(_: Request, exc: BadRequest) -> JSONResponse:
        return _error(str(exc), exc.status)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({}, status_code=200 if worker.alive() else 503)

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": served_model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "vibesys",
                    "root": str(engine.model_dir),
                    "parent": None,
                    "max_model_len": engine.max_model_len,
                }
            ],
        }

    @app.post("/v1/completions")
    async def completions(req: CompletionRequest, raw: Request):
        params = params_of(req)
        prompt_ids = prompt_ids_of(req)
        if len(prompt_ids) + params.max_tokens > engine.max_model_len:
            raise BadRequest(
                f"prompt ({len(prompt_ids)} tokens) + max_tokens ({params.max_tokens}) "
                f"exceeds max_model_len {engine.max_model_len}"
            )
        request_id = f"cmpl-{raw.headers.get('x-request-id') or uuid.uuid4().hex}"
        created = int(time.time())
        want_prompt_lps = req.echo and req.logprobs is not None

        def run(emit: Callable[[Any], None], cancelled: Callable[[], bool]) -> None:
            if want_prompt_lps:
                emit(("prompt_logprobs", engine.score(prompt_ids, top_k=req.logprobs or 0)))
            for step in engine.generate(prompt_ids, params):
                emit(step)
                if cancelled():
                    return

        items, cancel = await _submit(worker, run)
        base = {
            "id": request_id,
            "object": "text_completion",
            "created": created,
            "model": served_model_name,
        }

        if req.stream:
            return StreamingResponse(
                _stream(items, cancel, base, req, prompt_ids, tok), media_type="text/event-stream"
            )

        detok = Detokenizer(tok)
        lp_builder = (
            LogprobsBuilder(tok, req.return_tokens_as_token_ids)
            if req.logprobs is not None
            else None
        )
        text_parts: list[str] = []
        token_ids: list[int] = []
        finish = "length"
        try:
            async for item in items:
                if isinstance(item, tuple):  # prompt logprobs for echo
                    assert lp_builder is not None
                    lp_builder.add(prompt_ids[0], None)
                    for tid, lp in zip(prompt_ids[1:], item[1], strict=True):
                        lp_builder.add(tid, lp)
                    continue
                step: StepOutput = item
                token_ids.append(step.token_id)
                text_parts.append(detok.push(step.token_id, final=step.finish_reason is not None))
                if lp_builder is not None:
                    lp_builder.add(step.token_id, step.logprobs)
                if step.finish_reason is not None:
                    finish = step.finish_reason
        finally:
            cancel.set()
        text = "".join(text_parts)
        if req.echo:
            text = tok.decode(prompt_ids, skip_special_tokens=True) + text
        choice = {
            "index": 0,
            "text": text,
            "logprobs": lp_builder.as_dict() if lp_builder is not None else None,
            "finish_reason": finish,
            "stop_reason": None,
            "prompt_logprobs": None,
        }
        if req.return_token_ids:
            choice["prompt_token_ids"] = prompt_ids
            choice["token_ids"] = token_ids
        return {**base, "choices": [choice], "usage": _usage(len(prompt_ids), len(token_ids))}

    return app


async def _stream(
    items: AsyncIterator[Any],
    cancel: threading.Event,
    base: dict[str, Any],
    req: CompletionRequest,
    prompt_ids: list[int],
    tok,
) -> AsyncIterator[str]:
    """SSE stream: one chunk per generated token, optional usage chunk, then `[DONE]`."""
    include_usage = req.stream_options is not None and req.stream_options.include_usage
    continuous = (
        include_usage
        and req.stream_options is not None
        and req.stream_options.continuous_usage_stats
    )
    detok = Detokenizer(tok)
    n_out = 0
    try:
        async for item in items:
            step: StepOutput = item
            n_out += 1
            final = step.finish_reason is not None
            choice: dict[str, Any] = {
                "index": 0,
                "text": detok.push(step.token_id, final=final),
                "logprobs": None,
                "finish_reason": step.finish_reason,
                "stop_reason": None,
            }
            if req.logprobs is not None:
                lp = LogprobsBuilder(tok, req.return_tokens_as_token_ids)
                lp.add(step.token_id, step.logprobs)
                choice["logprobs"] = lp.as_dict()
            if req.return_token_ids:
                if n_out == 1:
                    choice["prompt_token_ids"] = prompt_ids
                choice["token_ids"] = [step.token_id]
            chunk = {**base, "choices": [choice]}
            if include_usage:
                chunk["usage"] = _usage(len(prompt_ids), n_out) if continuous else None
            yield f"data: {json.dumps(chunk)}\n\n"
        if include_usage:
            yield f"data: {json.dumps({**base, 'choices': [], 'usage': _usage(len(prompt_ids), n_out)})}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        log.exception("stream failed")
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'InternalServerError', 'code': 500}})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        cancel.set()  # client disconnect or completion: stop the worker's decode loop


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--model", required=True, help="HF repo id (resolved offline from HF_HOME) or local dir"
    )
    p.add_argument(
        "--served-model-name", default=None, help="name in /v1/models (default: --model)"
    )
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--max-model-len", type=int, default=32768, help="max prompt + generated tokens per request"
    )
    p.add_argument("--device", default="cuda")
    p.add_argument("--log-level", default="info")
    args = p.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    engine = Engine(args.model, device=args.device, max_model_len=args.max_model_len)
    list(
        engine.generate(engine.tokenizer("warmup").input_ids, SamplingParams(max_tokens=2))
    )  # first-call init
    app = build_app(engine, args.served_model_name or args.model)
    log.info(
        "ready: serving %s on %s:%d", args.served_model_name or args.model, args.host, args.port
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level, access_log=False)


if __name__ == "__main__":
    main()
