"""Loopback WebSocket gateway for browser presentation clients."""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from contextlib import suppress
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import TypeAdapter

from server.api.protocol import (
    EventBatchMessage,
    ProtocolErrorMessage,
    ProtocolRequest,
    Response,
    SubscribedMessage,
    SubscribeRequest,
)
from server.transport.subscriptions import SubscriptionTracker

if TYPE_CHECKING:
    from pathlib import Path

    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request
    from websockets.http11 import Response as HttpResponse

    from server.api.service import RunApi, SubscriptionBootstrap

_REQUEST_ADAPTER = TypeAdapter(ProtocolRequest)
_DISCONNECT_POLL_SECONDS = 0.1
_ALLOWED_ORIGIN_TEMPLATE = "http://127.0.0.1:{port}"
_WEB_SOCKET_PATH = "/ws"


class WebSocketGateway:
    """Serve the existing protocol API and web assets on one loopback port.

    WebSocket connections retain the Unix transport's role model: ordinary
    requests are handled serially on one connection, subscriptions take over
    their connection, and chat is free to occupy a dedicated connection. The
    gateway only changes framing, from JSONL lines to one text frame per
    protocol message. The API and subscription tracker remain shared with the
    Unix adapter.
    """

    def __init__(
        self,
        api: RunApi,
        *,
        assets_dir: Path | None = None,
        port: int = 0,
        subscriptions: SubscriptionTracker | None = None,
        token: str | None = None,
    ) -> None:
        """Create a loopback gateway around a shared run API."""
        self.api = api
        self.assets_dir = assets_dir.resolve() if assets_dir is not None else None
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self.subscriptions = subscriptions or SubscriptionTracker()
        self._server: object | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._bound_port: int | None = None

    @property
    def url(self) -> str:
        """Return the capability-bearing page URL after startup."""
        if self._bound_port is None:
            raise RuntimeError("WebSocket gateway is not running")  # noqa: TRY003
        return f"http://127.0.0.1:{self._bound_port}/?token={self.token}"

    @property
    def websocket_url(self) -> str:
        """Return the capability-bearing WebSocket endpoint after startup."""
        if self._bound_port is None:
            raise RuntimeError("WebSocket gateway is not running")  # noqa: TRY003
        return f"ws://127.0.0.1:{self._bound_port}{_WEB_SOCKET_PATH}?token={self.token}"

    @property
    def bound_port(self) -> int:
        """Return the actual listening port after startup."""
        if self._bound_port is None:
            raise RuntimeError("WebSocket gateway is not running")  # noqa: TRY003
        return self._bound_port

    def start(self) -> None:
        """Bind loopback and wait until the port is accepting connections."""
        if self._thread is not None:
            raise RuntimeError("WebSocket gateway is already running")  # noqa: TRY003
        self._thread = threading.Thread(
            target=self._run,
            name="vibesys-server-websocket",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=10)
        if not self._ready.is_set():
            raise RuntimeError("Timed out starting WebSocket gateway")  # noqa: TRY003
        if self._startup_error is not None:
            raise RuntimeError("Unable to start WebSocket gateway") from self._startup_error  # noqa: TRY003

    def close(self) -> None:
        """Stop the gateway and join its event-loop thread."""
        self._stop.set()
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: None)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._server = None
        self._bound_port = None

    def __enter__(self) -> WebSocketGateway:
        """Start and return the gateway."""
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Close the gateway after the context exits."""
        self.close()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve_until_stopped())
        except BaseException as error:  # noqa: BLE001
            self._startup_error = error
            self._ready.set()
        finally:
            loop.close()

    async def _serve_until_stopped(self) -> None:
        try:
            from websockets.asyncio.server import serve  # noqa: PLC0415
        except ImportError as error:  # pragma: no cover - packaging failure
            raise RuntimeError(  # noqa: TRY003
                "The WebSocket gateway requires the websockets package"
            ) from error

        async with serve(
            self._handle_connection,
            "127.0.0.1",
            self.port,
            process_request=self._process_request,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            max_queue=(32, 8),
            server_header="VibeSys-WebSocket",
        ) as server:
            self._server = server
            sockets = server.sockets
            if not sockets:
                raise RuntimeError(  # noqa: TRY003
                    "WebSocket gateway did not expose a listening socket"
                )
            self._bound_port = int(next(iter(sockets)).getsockname()[1])
            self._ready.set()
            await asyncio.to_thread(self._stop.wait)

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> HttpResponse | None:
        path = getattr(request, "path", "")
        parsed = urlsplit(path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        token = query.get("token", [""])[0]
        token_required = not parsed.path.startswith("/assets/")
        if token_required and not secrets.compare_digest(token, self.token):
            return _respond(connection, HTTPStatus.FORBIDDEN, "Invalid VibeSys capability token\n")

        if parsed.path == _WEB_SOCKET_PATH:
            origin = _request_header(request, "Origin")
            allowed_origin = self._actual_origin()
            if origin != allowed_origin:
                return _respond(connection, HTTPStatus.FORBIDDEN, "Invalid WebSocket origin\n")
            return None

        if parsed.path in {"/", "/index.html"}:
            return self._asset_response("index.html", cache_control="no-store")
        if not parsed.path.startswith("/assets/"):
            return _respond(connection, HTTPStatus.NOT_FOUND, "Not found\n")
        return self._asset_response(
            unquote(parsed.path.removeprefix("/")), cache_control="no-store"
        )

    def _actual_origin(self) -> str:
        if self._bound_port is None:
            return _ALLOWED_ORIGIN_TEMPLATE.format(port=self.port)
        return _ALLOWED_ORIGIN_TEMPLATE.format(port=self._bound_port)

    def _asset_response(self, relative: str, *, cache_control: str) -> HttpResponse:
        if self.assets_dir is None:
            return _response(HTTPStatus.NOT_FOUND, "Web assets are not installed\n", "text/plain")
        candidate = (self.assets_dir / relative).resolve()
        try:
            candidate.relative_to(self.assets_dir)
        except ValueError:
            return _response(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain")
        if not candidate.is_file():
            return _response(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain")
        try:
            body = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _response(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to read asset\n", "text/plain"
            )
        response = _response(HTTPStatus.OK, body, _content_type(candidate))
        response.headers["Cache-Control"] = cache_control  # type: ignore[attr-defined]
        return response

    async def _handle_connection(self, connection: ServerConnection) -> None:
        websocket = connection
        try:
            async for raw in websocket:
                if not isinstance(raw, str):
                    await websocket.send(
                        ProtocolErrorMessage(
                            code="invalid_frame",
                            message="WebSocket frames must contain text",
                        ).model_dump_json()
                    )
                    continue
                if await self._handle_request(websocket, raw):
                    return
        except Exception as error:  # noqa: BLE001
            # The websocket library owns close frames. A peer disappearing
            # while the API is writing is therefore a normal stream teardown.
            with suppress(Exception):
                await websocket.close()
            del error

    async def _handle_request(self, websocket: ServerConnection, raw: str) -> bool:
        request_id = _request_id(raw)
        try:
            request = _REQUEST_ADAPTER.validate_json(raw)
            if isinstance(request, SubscribeRequest):
                with self.subscriptions.track():
                    await self._stream(websocket, request)
                return True
            response = self.api.execute(request)
        except Exception as error:  # noqa: BLE001
            response = Response.from_exception(request_id, error, operation="Request")
        await websocket.send(response.model_dump_json())
        return False

    async def _stream(self, websocket: ServerConnection, request: SubscribeRequest) -> None:
        try:
            bootstrap = self.api.subscription_bootstrap(
                request.after_sequence, request.tail, store_id=request.store_id
            )
        except Exception as error:  # noqa: BLE001
            await websocket.send(
                ProtocolErrorMessage.from_exception(
                    error,
                    operation="Event stream",
                    code="stream_failed",
                    request_id=request.request_id,
                ).model_dump_json()
            )
            return
        await websocket.send(
            SubscribedMessage(
                request_id=request.request_id,
                run_id=bootstrap.run_id,
                latest_sequence=bootstrap.through_sequence,
            ).model_dump_json()
        )
        cursor, reported_floor, store_id = await self._write_bootstrap(
            websocket, request, bootstrap
        )
        while True:
            changed = await asyncio.to_thread(
                self.api.wait_for_change, cursor, _DISCONNECT_POLL_SECONDS
            )
            if not changed:
                if _connection_closed(websocket):
                    return
                continue
            if request.tail is not None and self.api.latest_sequence - cursor > request.tail:
                bootstrap = await asyncio.to_thread(
                    self.api.subscription_bootstrap,
                    request.after_sequence,
                    request.tail,
                    store_id=request.store_id,
                )
                cursor, reported_floor, store_id = await self._write_bootstrap(
                    websocket, request, bootstrap
                )
                continue
            checkpoint = await asyncio.to_thread(
                self.api.subscription_checkpoint, cursor, store_id=store_id
            )
            if checkpoint.store_id != store_id:
                bootstrap = await asyncio.to_thread(
                    self.api.subscription_bootstrap,
                    request.after_sequence,
                    request.tail,
                    store_id=request.store_id,
                )
                cursor, reported_floor, store_id = await self._write_bootstrap(
                    websocket, request, bootstrap
                )
                continue
            await websocket.send(
                EventBatchMessage(
                    events=checkpoint.events,
                    through_sequence=checkpoint.through_sequence,
                    active_executions=checkpoint.active_executions,
                    history_after_sequence=reported_floor,
                    store_id=store_id,
                ).model_dump_json()
            )
            cursor = checkpoint.through_sequence

    async def _write_bootstrap(
        self,
        websocket: ServerConnection,
        request: SubscribeRequest,
        bootstrap: SubscriptionBootstrap,
    ) -> tuple[int, int, str]:
        reported_floor = 0 if request.tail is None else bootstrap.floor
        await websocket.send(
            EventBatchMessage(
                events=bootstrap.events,
                through_sequence=bootstrap.through_sequence,
                active_executions=bootstrap.active_executions,
                history_after_sequence=reported_floor,
                store_id=bootstrap.store_id,
            ).model_dump_json()
        )
        return bootstrap.through_sequence, reported_floor, bootstrap.store_id


def _request_id(raw: str) -> str:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return "unknown"
    return str(value.get("request_id", "unknown")) if isinstance(value, dict) else "unknown"


def _request_header(request: Request, name: str) -> str | None:
    headers = getattr(request, "headers", {})
    return headers.get(name)


def _respond(_connection: ServerConnection, status: HTTPStatus, text: str) -> HttpResponse:
    return _response(status, text, "text/plain")


def _response(status: HTTPStatus, text: str, content_type: str) -> HttpResponse:
    from websockets.datastructures import Headers  # noqa: PLC0415
    from websockets.http11 import Response as HttpResponse  # noqa: PLC0415

    body = text.encode("utf-8")
    return HttpResponse(
        status.value,
        status.phrase,
        Headers(
            {
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
            }
        ),
        body,
    )


def _content_type(path: Path) -> str:
    return {
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
    }.get(path.suffix, "application/octet-stream")


def _connection_closed(connection: object) -> bool:
    state = getattr(connection, "state", None)
    return str(state).endswith("CLOSED")
