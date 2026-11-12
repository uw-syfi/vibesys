"""OpenAI-compatible HTTP server for the baseline engine.

    python -m engine.server [--host 127.0.0.1] [--port 8000]

Serves ``GET /health``, ``GET /v1/models``, ``POST /v1/completions`` and
``POST /v1/chat/completions``, streaming (SSE) or not. Connection threads parse
requests and write responses; one worker thread owns the model. It repeats one
loop: prefill each newly arrived request, one at a time, then pick one token
for every running request and feed all of them back in a single batched decode
step. The port opens only after the weights are loaded, so a readiness probe on
``/health`` passes once the server can answer.
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from engine import experts, protocol
from engine.generator import CHECKPOINT, Generation, Generator, Step
from engine.protocol import ChatRequest, CompletionRequest, Envelope, RequestError, Usage
from engine.text import Detokenizer, StopMatcher

_DONE = object()


@dataclass(frozen=True)
class AwaitDecode:
    """Yielded by a job after it has handled one token and needs the next decode step."""

    generation: Generation


@dataclass
class Job:
    """A request handed to the worker; the worker answers on ``events``.

    ``run`` yields the request's response events, and an ``AwaitDecode`` after
    each token that is not its last.
    """

    run: Callable[[], Iterator[dict[str, Any] | AwaitDecode]]
    events: queue.Queue = field(default_factory=queue.Queue)
    cancelled: threading.Event = field(default_factory=threading.Event)


def _internal(exc: Exception) -> RequestError:
    return RequestError(f"internal error: {type(exc).__name__}: {exc}", 500)


class Worker:
    """The only thread that touches the model: prefills new jobs, then decodes all running jobs together."""

    def __init__(self, generator: Generator) -> None:
        self._generator = generator
        self._jobs: queue.Queue[Job] = queue.Queue()
        threading.Thread(target=self._loop, name="engine-worker", daemon=True).start()

    def submit(self, job: Job) -> None:
        self._jobs.put(job)

    @staticmethod
    def _advance(job: Job, events: Iterator[dict[str, Any] | AwaitDecode]) -> Generation | None:
        """Run ``job`` up to its next decode step; None once it has ended."""
        try:
            for event in events:
                if job.cancelled.is_set():
                    break
                if isinstance(event, AwaitDecode):
                    return event.generation
                job.events.put(event)
        except RequestError as exc:
            job.events.put(exc)
        except Exception as exc:  # report to the client; the worker keeps serving
            job.events.put(_internal(exc))
        job.events.put(_DONE)
        return None

    def _loop(self) -> None:
        running: list[tuple[Job, Iterator[dict[str, Any] | AwaitDecode]]] = []
        while True:
            block = not running
            while True:
                try:
                    job = self._jobs.get(block=block)
                except queue.Empty:
                    break
                block = False
                running.append((job, job.run()))
            waiting = []
            for job, events in running:
                generation = self._advance(job, events)
                if generation is not None:
                    waiting.append((job, events, generation))
            running = [(job, events) for job, events, _ in waiting]
            if not waiting:
                continue
            try:
                self._generator.decode([generation for _, _, generation in waiting])
            except Exception as exc:  # the step failed for every request in it
                for job, _, _ in waiting:
                    job.events.put(_internal(exc))
                    job.events.put(_DONE)
                running = []


class _Output:
    """Splits generated tokens into reasoning and content text, with stop strings applied."""

    def __init__(self, generator: Generator, stops: tuple[str, ...], thinking: bool) -> None:
        self._generator = generator
        self._stops = stops
        self.in_reasoning = thinking
        self._detok = Detokenizer(generator.tokenizer)
        self._matcher = StopMatcher(stops)
        self.content: list[str] = []
        self.reasoning: list[str] = []

    @property
    def stopped(self) -> bool:
        return self._matcher.stopped

    def push(self, step: Step) -> tuple[str, str]:
        """(reasoning delta, content delta) for one step."""
        if not step.emit:
            return "", ""
        if self.in_reasoning and step.token_id == self._generator.think_end_id:
            tail = self._detok.flush()
            self.reasoning.append(tail)
            self.in_reasoning = False
            self._detok = Detokenizer(self._generator.tokenizer)
            return tail, ""
        text = self._detok.push(step.token_id)
        if self.in_reasoning:
            self.reasoning.append(text)
            return text, ""
        out = self._matcher.push(text)
        self.content.append(out)
        return "", out

    def flush(self) -> tuple[str, str]:
        tail = self._detok.flush()
        if self.in_reasoning:
            self.reasoning.append(tail)
            return tail, ""
        out = self._matcher.push(tail) + self._matcher.flush()
        self.content.append(out)
        return "", out


def _run(
    generation: Generation, output: _Output, usage: Usage
) -> Iterator[tuple[Step, str, str, protocol.FinishReason | None]]:
    """Yield (step, reasoning delta, content delta, finish) per token until the request ends.

    The caller yields ``AwaitDecode`` after each unfinished step, so the worker
    has decoded the token before this generator resumes.
    """
    while True:
        step = generation.next_step()
        usage.completion_tokens += int(step.emit)
        reasoning, content = output.push(step)
        finish = "stop" if output.stopped else step.finish
        if finish is not None:
            r_tail, c_tail = ("", "") if output.stopped else output.flush()
            yield step, reasoning + r_tail, content + c_tail, finish
            return
        yield step, reasoning, content, None


def completion_job(
    generator: Generator, model: str, request: CompletionRequest
) -> Iterator[dict[str, Any] | AwaitDecode]:
    """Events for ``/v1/completions``: chunks when streaming, else one response."""
    prompt = (
        generator.encode_text(request.prompt) if isinstance(request.prompt, str) else request.prompt
    )
    generation = generator.start(prompt, request.sampling)
    env = Envelope(model=model, kind="text_completion")
    usage = Usage(prompt_tokens=len(prompt), cached_tokens=generation.cached_tokens)
    output = _Output(generator, request.sampling.stop, thinking=False)
    token_ids: list[int] = []
    finish: protocol.FinishReason = "length"
    for step, _, text, finish_now in _run(generation, output, usage):
        if step.emit:
            token_ids.append(step.token_id)
        if request.stream:
            yield protocol.completion_chunk(
                env,
                text,
                step.token_id if step.emit else None,
                finish_now,
                request.return_token_ids,
            )
        finish = finish_now or finish
        if finish_now is None:
            yield AwaitDecode(generation)
    if request.stream:
        if request.include_usage:
            yield protocol.usage_chunk(env, usage)
    else:
        yield protocol.completion_response(
            env, "".join(output.content), token_ids, finish, usage, request.return_token_ids
        )


def chat_job(
    generator: Generator, model: str, request: ChatRequest
) -> Iterator[dict[str, Any] | AwaitDecode]:
    """Events for ``/v1/chat/completions``; reasoning streams as ``reasoning_content``."""
    prompt = generator.encode_chat(request)
    generation = generator.start(prompt, request.sampling)
    env = Envelope(model=model, kind="chat.completion")
    usage = Usage(prompt_tokens=len(prompt), cached_tokens=generation.cached_tokens)
    output = _Output(generator, request.sampling.stop, thinking=request.thinking)
    token_ids: list[int] = []
    finish: protocol.FinishReason = "length"
    if request.stream:
        yield protocol.chat_chunk(
            env, {"role": "assistant", "content": ""}, None, None, request.return_token_ids
        )
    for step, reasoning, content, finish_now in _run(generation, output, usage):
        if step.emit:
            token_ids.append(step.token_id)
        if request.stream:
            delta: dict[str, Any] = {"content": content}
            if reasoning:
                delta = {
                    "reasoning_content": reasoning,
                    **({"content": content} if content else {}),
                }
            yield protocol.chat_chunk(
                env,
                delta,
                step.token_id if step.emit else None,
                finish_now,
                request.return_token_ids,
            )
        finish = finish_now or finish
        if finish_now is None:
            yield AwaitDecode(generation)
    if request.stream:
        if request.include_usage:
            yield protocol.usage_chunk(env, usage)
    else:
        reasoning_text = "".join(output.reasoning) if request.thinking else None
        yield protocol.chat_response(
            env,
            "".join(output.content),
            reasoning_text,
            token_ids,
            finish,
            usage,
            request.return_token_ids,
        )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            g = self.server.generator
            stats = {
                "decode_steps": g.decode_steps,
                "decoded_tokens": g.decoded_tokens,
                "largest_batch": g.largest_batch,
            }
            self._send_json(200, {"status": "ok", **stats})
        elif self.path == "/v1/models":
            card = {
                "id": self.server.model_name,
                "object": "model",
                "owned_by": "vibesys",
                "max_model_len": self.server.generator.max_model_len,
            }
            self._send_json(200, {"object": "list", "data": [card]})
        else:
            self._send_json(404, protocol.error_body(f"no route {self.path}", 404))

    def do_POST(self) -> None:
        routes = {
            "/v1/completions": (protocol.parse_completion, completion_job),
            "/v1/chat/completions": (protocol.parse_chat, chat_job),
        }
        if self.path not in routes:
            self._send_json(404, protocol.error_body(f"no route {self.path}", 404))
            return
        parse, job_of = routes[self.path]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"null")
            protocol.check_model(body if isinstance(body, dict) else {}, self.server.model_name)
            request = parse(body)
        except json.JSONDecodeError:
            self._send_json(400, protocol.error_body("request body is not JSON", 400))
            return
        except RequestError as exc:
            self._send_json(exc.status, protocol.error_body(str(exc), exc.status))
            return
        generator, model = self.server.generator, self.server.model_name
        job = Job(run=lambda: job_of(generator, model, request))
        self.server.worker.submit(job)
        self._respond(job, stream=request.stream)

    def _respond(self, job: Job, *, stream: bool) -> None:
        first = job.events.get()
        if isinstance(first, RequestError):
            self._send_json(first.status, protocol.error_body(str(first), first.status))
            self._drain(job)
            return
        if not stream:
            self._send_json(200, first)
            self._drain(job)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        event = first
        try:
            while event is not _DONE:
                if isinstance(event, RequestError):
                    payload = protocol.error_body(str(event), event.status)
                else:
                    payload = event
                self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
                self.wfile.flush()
                event = job.events.get()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:  # the client went away: stop generating for it
            job.cancelled.set()
            self._drain(job)

    @staticmethod
    def _drain(job: Job) -> None:
        while job.events.get() is not _DONE:
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], generator: Generator, model_name: str) -> None:
        super().__init__(address, _Handler)
        self.generator = generator
        self.model_name = model_name
        self.worker = Worker(generator)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model", default=CHECKPOINT["model"], help="Hugging Face id or local directory"
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="checkpoint revision (default: the pinned one for the default model)",
    )
    parser.add_argument(
        "--served-model-name", default=None, help="name clients send as `model` (default: --model)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-model-len", type=int, default=262144)
    parser.add_argument("--prefill-chunk-tokens", type=int, default=2048)
    parser.add_argument(
        "--prefix-cache-tokens", type=int, default=262144, help="tokens of snapshots kept"
    )
    parser.add_argument(
        "--prefix-cache-entries", type=int, default=16, help="snapshots kept (0 disables the cache)"
    )
    parser.add_argument(
        "--experts-implementation",
        choices=("eager", "grouped_mm", "batched_mm", experts.NAME),
        default=None,
        help=f"Transformers MoE experts implementation (default: {experts.NAME} on ROCm, Transformers' own elsewhere)",
    )
    args = parser.parse_args(argv)
    revision = args.revision
    if revision is None and args.model == CHECKPOINT["model"]:
        revision = CHECKPOINT["revision"]
    generator = Generator.load(
        args.model,
        revision,
        max_model_len=args.max_model_len,
        chunk_tokens=args.prefill_chunk_tokens,
        prefix_cache_tokens=args.prefix_cache_tokens,
        prefix_cache_entries=args.prefix_cache_entries,
        experts_impl=args.experts_implementation,
    )
    server = _Server((args.host, args.port), generator, args.served_model_name or args.model)
    print(f"engine: serving {args.model} on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
