"""OpenAI-style chat server around the seed model.

    python3 server.py --model-path <dir> --host 0.0.0.0 --port 8000

One worker thread owns the model and serves requests FIFO, one at a time.
GET /health is 503 until the weights are loaded and a warmup forward succeeded.
"""

import argparse
import asyncio
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path

import torch
from aiohttp import web
from model import Model, load_cfg
from transformers import AutoTokenizer


class Engine:
    """Loads the model in the background and runs generation jobs FIFO on a worker thread."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tok = AutoTokenizer.from_pretrained(args.model_path)
        self.stop_ids = self._stop_ids(args.model_path)
        self.model: Model | None = None
        self.error: str | None = None
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def _stop_ids(self, path: str) -> frozenset[int]:
        ids = set(load_cfg(path).eos)
        gen = Path(path) / "generation_config.json"
        if gen.exists():
            eos = json.loads(gen.read_text()).get("eos_token_id", [])
            ids |= {eos} if isinstance(eos, int) else set(eos)
        if self.tok.eos_token_id is not None:
            ids.add(self.tok.eos_token_id)
        return frozenset(ids)

    @property
    def ready(self) -> bool:
        return self.model is not None

    def _run(self) -> None:
        try:
            a = self.args
            devices = a.devices.split(",") if a.devices else _default_devices()
            dtype = torch.bfloat16 if a.dtype == "bfloat16" else torch.float32
            model = Model(a.model_path, devices, dtype, a.max_seq_len)
            model.warmup()
            self.model = model
        except Exception as e:  # noqa: BLE001
            self.error = repr(e)
            raise
        while True:
            self.jobs.get()()  # each job is a closure that generates and reports back

    def submit(self, job) -> None:  # noqa: ANN001
        self.jobs.put(job)


def _default_devices() -> list[str]:
    n = torch.cuda.device_count()  # ROCm reports through torch.cuda too
    return [f"cuda:{i}" for i in range(n)] if n else ["cpu"]


class Detok:
    """Incremental detokenizer: text delta per token, holding back incomplete UTF-8."""

    def __init__(self, tok) -> None:  # noqa: ANN001
        self.tok, self.ids, self.sent = tok, [], ""

    def push(self, token: int) -> str:
        self.ids.append(token)
        text = self.tok.decode(self.ids, skip_special_tokens=True)
        if text.endswith("�"):
            return ""
        delta, self.sent = text[len(self.sent) :], text
        return delta


def build_prompt(engine: Engine, body: dict) -> list[int]:
    kwargs = body.get("chat_template_kwargs") or {}
    text = engine.tok.apply_chat_template(
        body["messages"], tokenize=False, add_generation_prompt=True, **kwargs
    )
    return engine.tok(text, add_special_tokens=False)["input_ids"]


def make_job(
    engine: Engine,
    prompt: list[int],
    body: dict,
    out: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
):
    """Closure run on the worker thread; pushes ('tok', text) then ('end', finish_reason)."""
    max_new = min(int(body.get("max_tokens") or 256), engine.args.max_seq_len - len(prompt))
    temperature = float(body.get("temperature") or 0.0)
    stop = frozenset() if body.get("ignore_eos") else engine.stop_ids

    def put(item) -> None:  # noqa: ANN001
        loop.call_soon_threadsafe(out.put_nowait, item)

    def job() -> None:
        try:
            detok, n, reason = Detok(engine.tok), 0, "length"
            for token in engine.model.generate(prompt, max_new, temperature, stop):
                n += 1
                if token in stop:
                    reason = "stop"
                    break
                put(("tok", detok.push(token)))
            put(("end", (reason, n)))
        except Exception as e:  # noqa: BLE001
            put(("error", repr(e)))

    return job


def usage(prompt: list[int], n: int) -> dict:
    return {"prompt_tokens": len(prompt), "completion_tokens": n, "total_tokens": len(prompt) + n}


def chunk(
    rid: str, model: str, delta: dict, finish: str | None = None, usage_: dict | None = None
) -> str:
    obj = {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    obj["choices"] = [] if usage_ else [{"index": 0, "delta": delta, "finish_reason": finish}]
    if usage_:
        obj["usage"] = usage_
    return f"data: {json.dumps(obj)}\n\n"


async def chat(request: web.Request) -> web.StreamResponse:
    engine: Engine = request.app["engine"]
    if not engine.ready:
        return web.json_response({"error": "model not ready"}, status=503)
    body = await request.json()
    prompt = build_prompt(engine, body)
    if len(prompt) >= engine.args.max_seq_len:
        return web.json_response(
            {"error": f"prompt has {len(prompt)} tokens, max_seq_len is {engine.args.max_seq_len}"},
            status=400,
        )
    out: asyncio.Queue = asyncio.Queue()
    engine.submit(make_job(engine, prompt, body, out, asyncio.get_running_loop()))
    rid, name = f"chatcmpl-{uuid.uuid4().hex}", body.get("model") or "qwen3.5-397b-a17b-mxfp4"
    if body.get("stream"):
        return await stream_reply(
            request,
            out,
            prompt,
            rid,
            name,
            (body.get("stream_options") or {}).get("include_usage", False),
        )
    text, kind, val = "", "", None
    while kind not in ("end", "error"):
        kind, val = await out.get()
        text += val if kind == "tok" else ""
    if kind == "error":
        return web.json_response({"error": val}, status=500)
    reason, n = val
    msg = {"role": "assistant", "content": text}
    resp = {"id": rid, "object": "chat.completion", "created": int(time.time()), "model": name}
    resp["choices"] = [{"index": 0, "message": msg, "finish_reason": reason}]
    resp["usage"] = usage(prompt, n)
    return web.json_response(resp)


async def stream_reply(request, out, prompt, rid, name, include_usage) -> web.StreamResponse:  # noqa: ANN001, FBT001
    resp = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
    )
    await resp.prepare(request)
    first = True
    while True:
        kind, val = await out.get()
        if kind == "tok":  # one SSE chunk per generated token
            delta = {"content": val} | ({"role": "assistant"} if first else {})
            first = False
            await resp.write(chunk(rid, name, delta).encode())
            continue
        if kind == "end":
            reason, n = val
            await resp.write(chunk(rid, name, {}, finish=reason).encode())
            if include_usage:
                await resp.write(chunk(rid, name, {}, usage_=usage(prompt, n)).encode())
        else:
            await resp.write(f"data: {json.dumps({'error': val})}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp


async def health(request: web.Request) -> web.Response:
    engine: Engine = request.app["engine"]
    if engine.ready:
        return web.json_response({"status": "ok"})
    return web.json_response({"status": "loading", "error": engine.error}, status=503)


async def models(_request: web.Request) -> web.Response:
    return web.json_response(
        {"object": "list", "data": [{"id": "qwen3.5-397b-a17b-mxfp4", "object": "model"}]}
    )


def make_app(args: argparse.Namespace) -> web.Application:
    app = web.Application()
    app["engine"] = Engine(args)
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/v1/models", models),
            web.post("/v1/chat/completions", chat),
        ]
    )
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--model-path", default=os.environ.get("MODEL_PATH"))
    p.add_argument("--host", default="0.0.0.0")  # noqa: S104
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--devices", default="", help="comma list, e.g. cuda:0,cuda:1 (default: all GPUs, else cpu)"
    )
    p.add_argument("--dtype", choices=["bfloat16", "float32"], default="bfloat16")
    p.add_argument("--max-seq-len", type=int, default=16384)
    args = p.parse_args(argv)
    if not args.model_path:
        p.error("--model-path or MODEL_PATH is required")
    return args


if __name__ == "__main__":
    a = parse_args()
    web.run_app(make_app(a), host=a.host, port=a.port)
