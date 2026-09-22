"""HTTP/WebSocket adapter for the existing Unix JSONL backend protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web
from pydantic import TypeAdapter, ValidationError

from server.api.protocol import (
    ChatQuery,
    ProtocolErrorMessage,
    ProtocolRequest,
    Response,
    SubscribeRequest,
)
from server.diagnostics import DiagnosticScope

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_REQUEST_ADAPTER = TypeAdapter(ProtocolRequest)
MAX_REQUEST_BYTES = 1 << 20
# Replay batches and snapshots can exceed asyncio's default 64 KiB line limit.
MAX_UPSTREAM_LINE_BYTES = 64 << 20
_KEEPALIVE_RETRY_SECONDS = 1.0
_UPSTREAM_CONNECT_TIMEOUT_SECONDS = 5.0


def default_allowed_origins(
    host: str, port: int, *, dev_ports: Iterable[int] = ()
) -> frozenset[str]:
    """Allow the gateway origin and explicitly selected loopback dev ports."""
    authority = f"[{host}]" if ":" in host else host
    origins = {f"http://{authority}:{port}"}
    for origin_port in (port, *dev_ports):
        for alias in ("127.0.0.1", "localhost"):
            origins.add(f"http://{alias}:{origin_port}")
    return frozenset(origins)


def _origin_allowed(origin: str | None, allowed_origins: frozenset[str]) -> bool:
    return origin is None or origin in allowed_origins


def _error_response(
    request_id: str, error: BaseException, *, code: str, status: int
) -> web.Response:
    response = Response.from_exception(
        request_id, error, operation="Request", scope=DiagnosticScope.TRANSPORT, code=code
    )
    return web.Response(
        body=response.model_dump_json(), status=status, content_type="application/json"
    )


class BrowserGateway:
    """Own a standing subscription so browser refresh does not detach the run."""

    def __init__(self, socket_path: Path, allowed_origins: Iterable[str]) -> None:
        """Store upstream address and accepted browser origins."""
        self.socket_path = socket_path
        self.allowed_origins = frozenset(allowed_origins)
        self.websockets: set[web.WebSocketResponse] = set()
        self._keepalive_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the gateway-owned subscription once."""
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(self._run_keepalive())

    async def stop(self) -> None:
        """Close browser subscriptions and cancel the gateway-owned subscription."""
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(self.websockets)))
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task
            self._keepalive_task = None

    async def _run_keepalive(self) -> None:
        while True:
            try:
                reader, writer = await _open_upstream(self.socket_path)
                try:
                    writer.write(
                        SubscribeRequest(after_sequence=0, tail=1).model_dump_json().encode()
                        + b"\n"
                    )
                    await writer.drain()
                    while await reader.readline():
                        pass
                finally:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
            except (OSError, TimeoutError, ValueError):
                pass
            await asyncio.sleep(_KEEPALIVE_RETRY_SECONDS)


_GATEWAY_KEY: web.AppKey[BrowserGateway] = web.AppKey("gateway", BrowserGateway)


async def _open_upstream(socket_path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.wait_for(
        asyncio.open_unix_connection(path=str(socket_path), limit=MAX_UPSTREAM_LINE_BYTES),
        timeout=_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
    )


async def _handle_request(request: web.Request) -> web.Response:
    gateway = request.app[_GATEWAY_KEY]
    if not _origin_allowed(request.headers.get("Origin"), gateway.allowed_origins):
        raise web.HTTPForbidden(reason="Origin not allowed")
    body = await request.read()
    try:
        raw = json.loads(body)
        request_id = str(raw.get("request_id", "unknown")) if isinstance(raw, dict) else "unknown"
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _error_response("unknown", exc, code="invalid_request", status=400)
    try:
        parsed: ProtocolRequest = _REQUEST_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        return _error_response(request_id, exc, code="invalid_request", status=400)
    if isinstance(parsed, SubscribeRequest):
        return _error_response(
            parsed.request_id,
            ValueError("Subscribe over the WebSocket endpoint, not /api/request"),
            code="invalid_request",
            status=400,
        )
    try:
        reader, writer = await _open_upstream(gateway.socket_path)
    except (OSError, TimeoutError) as exc:
        return _error_response(parsed.request_id, exc, code="backend_unavailable", status=502)
    try:
        writer.write(parsed.model_dump_json().encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(
            reader.readline(), timeout=None if isinstance(parsed, ChatQuery) else 300
        )
        if not line:
            raise ConnectionError("Backend closed the connection before responding")  # noqa: TRY003
    except (OSError, TimeoutError, ValueError) as exc:
        return _error_response(parsed.request_id, exc, code="backend_unavailable", status=502)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    return web.Response(body=line, content_type="application/json")


async def _handle_events(request: web.Request) -> web.WebSocketResponse:
    gateway = request.app[_GATEWAY_KEY]
    if not _origin_allowed(request.headers.get("Origin"), gateway.allowed_origins):
        raise web.HTTPForbidden(reason="Origin not allowed")
    ws = web.WebSocketResponse(max_msg_size=MAX_REQUEST_BYTES)
    await ws.prepare(request)
    gateway.websockets.add(ws)
    try:
        return await _subscribe_events(ws, gateway.socket_path)
    finally:
        gateway.websockets.discard(ws)
        await ws.close()


async def _subscribe_events(ws: web.WebSocketResponse, socket_path: Path) -> web.WebSocketResponse:
    try:
        handshake = await ws.receive(timeout=5)
    except TimeoutError:
        await ws.close()
        return ws
    if handshake.type != WSMsgType.TEXT:
        await ws.close()
        return ws
    try:
        subscribe = SubscribeRequest.model_validate_json(handshake.data)
    except ValidationError as exc:
        await ws.send_str(
            ProtocolErrorMessage.from_exception(
                exc,
                operation="Event stream",
                scope=DiagnosticScope.PROTOCOL,
                code="invalid_subscribe",
            ).model_dump_json()
        )
        await ws.close()
        return ws
    try:
        reader, writer = await _open_upstream(socket_path)
    except (OSError, TimeoutError) as exc:
        await ws.send_str(
            ProtocolErrorMessage.from_exception(
                exc,
                request_id=subscribe.request_id,
                operation="Event stream",
                scope=DiagnosticScope.TRANSPORT,
                code="backend_unavailable",
            ).model_dump_json()
        )
        await ws.close()
        return ws
    try:
        writer.write(subscribe.model_dump_json().encode() + b"\n")
        await writer.drain()
        await _relay(ws, reader)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    return ws


async def _relay(ws: web.WebSocketResponse, reader: asyncio.StreamReader) -> None:
    async def upstream_to_browser() -> None:
        while line := await reader.readline():
            await ws.send_str(line.decode().rstrip("\n"))

    async def watch_browser_close() -> None:
        async for _msg in ws:
            pass

    tasks = (asyncio.create_task(upstream_to_browser()), asyncio.create_task(watch_browser_close()))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not ws.closed:
            await ws.close()


def build_gateway_app(socket_path: Path, *, allowed_origins: Iterable[str] = ()) -> web.Application:
    """Build the adapter; the entrypoint owns the listener and event loop."""
    gateway = BrowserGateway(socket_path, allowed_origins)
    app = web.Application(
        client_max_size=MAX_REQUEST_BYTES, handler_args={"handler_cancellation": True}
    )
    app[_GATEWAY_KEY] = gateway
    app.router.add_post("/api/request", _handle_request)
    app.router.add_get("/api/events", _handle_events)
    app.on_startup.append(lambda _app: gateway.start())
    app.on_shutdown.append(lambda _app: gateway.stop())
    app.on_cleanup.append(lambda _app: gateway.stop())
    return app
