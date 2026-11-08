"""The server: ``python -m engine.server --model-dir DIR --host 127.0.0.1 --port PORT``.

``GET /health`` answers 200 once the model is loaded; the port accepts no connection before.
``POST /v1/completions`` takes ``prompt``, ``max_tokens``, ``temperature``, ``stream`` and
``response_format`` (``{"type": "json_schema", "json_schema": {"schema": {...}}}``) and answers
with an OpenAI-style completion, or with Server-Sent Events when ``stream`` is true.

MLX streams belong to the thread that made them, so one thread, the main one, loads the model and
runs every generation, one at a time. The HTTP handlers run in other threads and hand it jobs.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from engine.constraint import SchemaError
from engine.generate import Engine, Piece

MODEL_NAME = "mlx-community/Meta-Llama-3.1-8B-Instruct-8bit"


class RequestError(ValueError):
    """The request body is not one this server accepts."""


def parse_request(body: Any) -> tuple[str, int, float, bool, dict[str, Any] | None]:
    """The prompt, token budget, temperature, streaming flag and schema of a completion request."""
    if not isinstance(body, dict):
        raise RequestError("the body must be a JSON object")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise RequestError("prompt must be a non-empty string")
    max_tokens = body.get("max_tokens", 16)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise RequestError("max_tokens must be a positive integer")
    temperature = body.get("temperature", 1.0)
    if isinstance(temperature, bool) or not isinstance(temperature, int | float) or temperature < 0:
        raise RequestError("temperature must be a number of at least 0")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise RequestError("stream must be a boolean")
    schema = None
    response_format = body.get("response_format")
    if response_format is not None:
        try:
            if response_format["type"] != "json_schema":
                raise RequestError('response_format.type must be "json_schema"')
            schema = response_format["json_schema"]["schema"]
        except (KeyError, TypeError) as error:
            raise RequestError("response_format must be {type, json_schema: {schema}}") from error
        if not isinstance(schema, dict):
            raise RequestError("response_format.json_schema.schema must be a JSON object")
    return prompt, max_tokens, float(temperature), stream, schema


def chunk(request_id: str, created: int, text: str, finish_reason: str | None) -> dict[str, Any]:
    """One OpenAI text_completion object, a streamed chunk or the whole reply."""
    return {
        "id": request_id,
        "object": "text_completion",
        "created": created,
        "model": MODEL_NAME,
        "choices": [{"index": 0, "text": text, "finish_reason": finish_reason}],
    }


@dataclass
class Job:
    """One generation, handed from a handler thread to the main thread.

    The main thread puts the pieces on ``pieces``, then the error that ended it early (if any), then
    None. The handler sets ``cancelled`` when the client is gone.
    """

    prompt: str
    max_tokens: int
    temperature: float
    schema: dict[str, Any] | None
    pieces: queue.Queue[Piece | Exception | None] = field(default_factory=queue.Queue)
    cancelled: threading.Event = field(default_factory=threading.Event)


def run_jobs(engine: Engine, jobs: queue.Queue[Job]) -> None:
    """Run the jobs, one at a time, on this thread. Does not return."""
    while True:
        job = jobs.get()
        try:
            for piece in engine.stream(
                job.prompt,
                max_tokens=job.max_tokens,
                temperature=job.temperature,
                schema=job.schema,
            ):
                if job.cancelled.is_set():
                    break
                job.pieces.put(piece)
        except Exception as error:  # reported to the client, which decides what to do with it
            job.pieces.put(error)
        finally:
            job.pieces.put(None)


class Handler(BaseHTTPRequestHandler):
    """One connection."""

    jobs: queue.Queue[Job]

    def log_message(self, format: str, *args: Any) -> None:
        """Per-request lines are noise in the log; failures are reported where they happen."""

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": {"message": message}})

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
        else:
            self._error(HTTPStatus.NOT_FOUND, f"no such path: {self.path}")

    def do_POST(self) -> None:
        if self.path != "/v1/completions":
            self._error(HTTPStatus.NOT_FOUND, f"no such path: {self.path}")
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            prompt, max_tokens, temperature, stream, schema = parse_request(body)
        except (ValueError, RequestError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        job = Job(prompt, max_tokens, temperature, schema)
        self.jobs.put(job)
        try:
            self._answer(job, stream, f"cmpl-{uuid.uuid4().hex}", int(time.time()))
        finally:
            job.cancelled.set()

    def _answer(self, job: Job, stream: bool, request_id: str, created: int) -> None:
        first = job.pieces.get()
        if isinstance(first, SchemaError):
            self._error(HTTPStatus.BAD_REQUEST, f"unsupported schema: {first}")
            return
        if isinstance(first, Exception):
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(first).__name__}: {first}")
            return
        if stream:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        pieces: list[Piece] = []
        item = first
        while item is not None:
            if isinstance(item, Exception):
                raise item
            if stream:
                event = chunk(request_id, created, item.text, item.finish_reason)
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            else:
                pieces.append(item)
            item = job.pieces.get()
        if stream:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            text = "".join(piece.text for piece in pieces)
            reason = pieces[-1].finish_reason if pieces else "stop"
            self._send_json(HTTPStatus.OK, chunk(request_id, created, text, reason))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    print(f"loading {args.model_dir}", file=sys.stderr, flush=True)
    engine = Engine(args.model_dir)
    Handler.jobs = queue.Queue()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"serving on {args.host}:{args.port}", file=sys.stderr, flush=True)
    run_jobs(engine, Handler.jobs)


if __name__ == "__main__":
    main()
