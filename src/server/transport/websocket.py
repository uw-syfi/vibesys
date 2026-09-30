"""Loopback WebSocket gateway for browser presentation clients."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import posixpath
import secrets
import threading
from contextlib import suppress
from http import HTTPStatus
from ipaddress import ip_address
from pathlib import Path
from string import ascii_lowercase, digits
from types import MappingProxyType
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
from server.transport.discovery import WebInstanceClaim, WebInstanceRecord
from server.transport.subscriptions import SubscriptionTracker

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request
    from websockets.http11 import Response as HttpResponse

    from server.api.service import RunApi, SubscriptionBootstrap

_REQUEST_ADAPTER = TypeAdapter(ProtocolRequest)
_DISCONNECT_POLL_SECONDS = 0.1
_LOOPBACK_HOST = "127.0.0.1"
# This gateway's own authority, spelled once. The page origin it accepts and
# the socket source its policy names are this single `host:port` under two
# schemes, so the two cannot come to name different authorities.
_AUTHORITY_TEMPLATE = f"{_LOOPBACK_HOST}:{{port}}"
_WEB_SOCKET_PATH = "/ws"
_ASSET_PREFIX = "/assets/"
# The one subdirectory of the bundle the `/assets/` URL space names, spelled
# once so the prefix test and the filesystem guard cannot disagree.
_ASSET_DIRECTORY = _ASSET_PREFIX.strip("/")

# The schemes a browser can name in an `Origin` header, each with the port it
# omits when the authority uses that scheme's default.
_ORIGIN_DEFAULT_PORTS: Mapping[str, int] = MappingProxyType({"http": 80, "https": 443})
# Every character a host in an `Origin` header can hold once `urlsplit` has
# lowercased it. Deliberately not the CSP `host-part` character set, which is
# narrower still (no brackets, so no IPv6 literal): declared origins are
# compared to a request header and never reach a response header.
_HOST_CHARACTERS = frozenset(ascii_lowercase + digits + "-.")

# The gateway renders model- and tool-produced transcript text, so the served
# page is denied every fetch destination by default and then granted exactly
# what the built bundle uses: its own script and stylesheet, and the WebSocket
# of the origin it was loaded from. This mapping is the one representation of
# the policy that the gateway serves; `_content_security_policy` is its only
# serializer. Two tests restate it as independent cross-checks
# (`_expected_policy` in `tests/server/test_websocket_transport.py` and
# `expectedPolicy` in `clients/web/e2e/gateway-hygiene.spec.ts`); neither
# imports this module, so neither is tautological, and both fail when they
# disagree with it.
_POLICY_DIRECTIVES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "default-src": ("'none'",),
        "script-src": ("'self'",),
        "style-src": ("'self'",),
        "img-src": ("'self'",),
        "font-src": ("'none'",),
        # `'self'` is load-bearing beyond the socket and must not be tightened
        # away: the bundle's first executing statement is Vite's modulepreload
        # polyfill, which `fetch`es every `link[rel=modulepreload]` href, and a
        # `fetch` of a script URL is governed by `connect-src`, not
        # `script-src`. Inert while the build emits a single chunk, live the
        # moment it code-splits.
        #
        # `_socket_source` appends this gateway's own `ws://host:port`, which
        # is what actually authorizes the socket. Every page this gateway
        # hands out is at that authority (`url` is always
        # `http://127.0.0.1:<bound port>/...`), so the grant is exact and
        # textual and does not rest on how an engine resolves `'self'` against
        # `ws:`, which CSP3 leaves less clear than it does for `wss:`. A page
        # reached by some other spelling of the same socket, or served through
        # a proxy forwarding these headers, falls back on `'self'`; see
        # `_socket_source` for why no declared origin can help there.
        "connect-src": ("'self'",),
        "base-uri": ("'none'",),
        "form-action": ("'none'",),
        "frame-ancestors": ("'none'",),
        "object-src": ("'none'",),
    }
)

# Response headers that do not depend on gateway state, merged once by
# `_response`, so `Cache-Control` has exactly one writer. `Referrer-Policy` is
# `no-referrer` because the page URL carries the capability token in its query
# string and a `Referer` header would copy it to whatever the page links or
# navigates to. `nosniff` is the policy's companion: `_content_type` falls back
# to `application/octet-stream`, and `/assets/*` is token-free, so no response
# may be re-typed by content sniffing into something the policy would execute.
_STATIC_HEADERS = MappingProxyType(
    {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
)


class WebSocketGateway:
    """Serve the existing protocol API and web assets on one loopback port.

    WebSocket connections retain the Unix transport's role model: ordinary
    requests are handled serially on one connection, subscriptions take over
    their connection, and chat is free to occupy a dedicated connection. The
    gateway only changes framing, from JSONL lines to one text frame per
    protocol message. The API and subscription tracker remain shared with the
    Unix adapter.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-101057 [PLR0913]; the gateway exposes independent protocol, asset, port, capability, and discovery options
        self,
        api: RunApi,
        *,
        assets_dir: Path | None = None,
        port: int = 0,
        subscriptions: SubscriptionTracker | None = None,
        token: str | None = None,
        instance_path: Path | None = None,
        project_root: Path | None = None,
        allowed_origins: Sequence[str] = (),
    ) -> None:
        """Create a loopback gateway around a shared run API.

        Raises `ValueError`, naming the offending value, when `allowed_origins`
        holds anything a browser cannot send as an `Origin` header.
        """
        self.api = api
        self.assets_dir = assets_dir.resolve() if assets_dir is not None else None
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self.instance_path = instance_path
        self.project_root = project_root or Path.cwd()
        # Parsed here because this is the one boundary declared origins enter
        # through, so the stored set holds the exact spelling a browser sends
        # and an origin no browser can send is a construction-time error rather
        # than an allowlist entry that silently never matches.
        self.allowed_origins = frozenset(browser_origin(origin) for origin in allowed_origins)
        self.subscriptions = subscriptions or SubscriptionTracker()
        self._claim: WebInstanceClaim | None = None
        self._instance_record: WebInstanceRecord | None = None
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
            raise RuntimeError("WebSocket gateway is not running")  # noqa: TRY003  # lint-waiver: LW-101007 [TRY003]; property misuse is a programmer error during gateway lifecycle
        return f"http://{_LOOPBACK_HOST}:{self._bound_port}/?token={self.token}"

    @property
    def websocket_url(self) -> str:
        """Return the capability-bearing WebSocket endpoint after startup."""
        if self._bound_port is None:
            raise RuntimeError("WebSocket gateway is not running")  # noqa: TRY003  # lint-waiver: LW-101008 [TRY003]; property misuse is a programmer error during gateway lifecycle
        return f"ws://{_LOOPBACK_HOST}:{self._bound_port}{_WEB_SOCKET_PATH}?token={self.token}"

    @property
    def bound_port(self) -> int:
        """Return the actual listening port after startup."""
        if self._bound_port is None:
            raise RuntimeError("WebSocket gateway is not running")  # noqa: TRY003  # lint-waiver: LW-101009 [TRY003]; property misuse is a programmer error during gateway lifecycle
        return self._bound_port

    def start(self) -> None:
        """Bind loopback and wait until the port is accepting connections."""
        if self._thread is not None:
            raise RuntimeError("WebSocket gateway is already running")  # noqa: TRY003  # lint-waiver: LW-101010 [TRY003]; reject a second start before it can race the event loop
        self._stop.clear()
        self._ready.clear()
        self._startup_error = None
        if self.instance_path is not None:
            claim = WebInstanceClaim(self.instance_path)
            if not claim.try_acquire():
                raise RuntimeError("Another VibeSys web gateway owns this project")  # noqa: TRY003  # lint-waiver: LW-101033 [TRY003]; reject a duplicate project-local web gateway
            self._claim = claim
        self._thread = threading.Thread(
            target=self._run,
            name="vibesys-server-websocket",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=10)
        if not self._ready.is_set():
            raise RuntimeError("Timed out starting WebSocket gateway")  # noqa: TRY003  # lint-waiver: LW-101011 [TRY003]; convert a startup synchronization timeout into a clear lifecycle error
        if self._startup_error is not None:
            self._release_instance()
            raise RuntimeError("Unable to start WebSocket gateway") from self._startup_error  # noqa: TRY003  # lint-waiver: LW-101012 [TRY003]; preserve the gateway startup failure as a lifecycle error

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
        self._release_instance()

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
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-101013 [BLE001]; propagate any event-loop startup failure through the owning thread
            self._startup_error = error
            self._ready.set()
        finally:
            loop.close()

    async def _serve_until_stopped(self) -> None:
        try:
            from websockets.asyncio.server import (  # noqa: PLC0415  # lint-waiver: LW-101022 [PLC0415]; defer the optional websocket dependency until gateway startup
                serve,
            )
        except ImportError as error:  # pragma: no cover - packaging failure
            raise RuntimeError(  # noqa: TRY003  # lint-waiver: LW-101014 [TRY003]; give an actionable dependency error at gateway startup
                "The WebSocket gateway requires the websockets package"
            ) from error

        async with serve(
            self._handle_connection,
            _LOOPBACK_HOST,
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
                raise RuntimeError(  # noqa: TRY003  # lint-waiver: LW-101015 [TRY003]; fail startup if the websocket library did not bind a socket
                    "WebSocket gateway did not expose a listening socket"
                )
            self._bound_port = int(next(iter(sockets)).getsockname()[1])
            if self.instance_path is not None:
                self._instance_record = WebInstanceRecord.from_gateway(
                    pid=os.getpid(),
                    port=self._bound_port,
                    token=self.token,
                    project_root=self.project_root,
                )
                self._instance_record.write(self.instance_path)
            self._ready.set()
            await asyncio.to_thread(self._stop.wait)

    def _release_instance(self) -> None:
        record = self._instance_record
        if record is not None and self.instance_path is not None:
            record.remove_if_owner(self.instance_path)
        self._instance_record = None
        claim = self._claim
        self._claim = None
        if claim is not None:
            claim.close()

    async def _process_request(  # noqa: PLR0911  # lint-waiver: LW-101058 [PLR0911]; each HTTP route returns its precise status and body at this protocol boundary
        self, _connection: ServerConnection, request: Request
    ) -> HttpResponse | None:
        # `websockets` calls this with the connection positionally. Responses are
        # built from gateway state, not from the connection, so it is unused.
        #
        # Split, not `urlsplit`: an origin-form request target is
        # `absolute-path [ "?" query ]` (RFC 9110 7.1) and not a URL, so `#` is
        # an ordinary query byte rather than a fragment delimiter, and a target
        # a URL parser rejects is still a target this gateway has to answer.
        # `urlsplit` raised `ValueError` on `//[::/` and on any absolute-form
        # target with a malformed authority, which left the handshake through
        # the library's error path as an unauthenticated 500 carrying none of
        # these headers. `partition` cannot fail.
        raw_path, _separator, raw_query = getattr(request, "path", "").partition("?")
        query = parse_qs(raw_query, keep_blank_values=True)
        token = query.get("token", [""])[0]
        # Route on one normalized target. Deciding the token requirement from the
        # raw path and then looking the file up from a decoded, traversal-bearing
        # copy of it would let `/assets/../index.html` claim the `/assets/`
        # exemption while resolving to a file outside it.
        path = _routing_path(raw_path)
        serves_asset = path.startswith(_ASSET_PREFIX)
        # Compared as bytes, encoded with the inverse of the decode the library
        # applied. `compare_digest` raises `TypeError` on a `str` holding a
        # non-ASCII character, and `token` is whatever the query string carried,
        # so a plain comparison made `?token=%C3%A9` a library-generated 500.
        # `websockets` decodes the target with `ascii`/`surrogateescape`
        # (`websockets/http11.py`), so a raw byte above 0x7F arrives as a lone
        # surrogate that strict UTF-8 also refuses; `surrogateescape` here is
        # that decode's inverse and recovers the byte the client sent. Bytes
        # keep the comparison constant-time, and one encoder is used on both
        # sides so a caller-supplied token cannot fail where a request cannot.
        candidate = token.encode("utf-8", "surrogateescape")
        expected = self.token.encode("utf-8", "surrogateescape")
        if not serves_asset and not secrets.compare_digest(candidate, expected):
            return self._response(
                HTTPStatus.FORBIDDEN, "Invalid VibeSys capability token\n", "text/plain"
            )

        if path == _WEB_SOCKET_PATH:
            origin = _request_header(request, "Origin")
            if origin not in self._allowed_origins():
                return self._response(
                    HTTPStatus.FORBIDDEN, "Invalid WebSocket origin\n", "text/plain"
                )
            return None

        if path == "/health":
            return self._response(HTTPStatus.OK, "vibesys-ok\n", "text/plain")

        if path in {"/", "/index.html"}:
            return self._asset_response("index.html")
        if not serves_asset:
            return self._response(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain")
        return self._asset_response(path.removeprefix(_ASSET_PREFIX), subdirectory=_ASSET_DIRECTORY)

    def _authority(self) -> str:
        """Return this gateway's own `host:port`, falling back before the bind."""
        port = self.port if self._bound_port is None else self._bound_port
        return _AUTHORITY_TEMPLATE.format(port=port)

    def _actual_origin(self) -> str:
        return f"http://{self._authority()}"

    def _allowed_origins(self) -> frozenset[str]:
        return frozenset({self._actual_origin(), *self.allowed_origins})

    def _socket_source(self) -> str:
        """Return the one WebSocket authority this gateway's policy names.

        Declared browser origins are deliberately absent, because they answer
        a different question. `allowed_origins` is the inbound `Origin`
        allowlist: it says who may connect *to* this gateway. `connect-src`
        says where a page this gateway *served* may connect to. Neither
        documented flow makes a declared origin an answer to the second:

        - In the development flow the page is served by Vite, so it carries
          Vite's policy and this header never governs it.
        - In a proxied deployment the page is at the proxy's authority and its
          socket uses the proxy's own port, never this gateway's, so pairing a
          declared *hostname* with this gateway's bound port (what this
          replaced) named an authority no page can ever be served from.

        So a declared origin cannot name a source a gateway-served page needs,
        and none reaches the header. That is also why no configured string can
        put a wildcard host, a space, or a directive-terminating `;` into a
        security header: the value is built from module constants and the
        bound socket, and nothing else.

        The residual is a page at some other spelling of this socket, or
        behind a header-forwarding proxy: its socket rests on `'self'`, which
        CSP3 states covers the `wss:` variant of the page's origin but is less
        clear about plain `ws:`. Deriving this from the document request's own
        authority would close that; it is not done here.
        """
        return f"ws://{self._authority()}"

    def _content_security_policy(self) -> str:
        """Serialize `_POLICY_DIRECTIVES` with this instance's socket source."""
        directives = dict(_POLICY_DIRECTIVES)
        directives["connect-src"] = (*directives["connect-src"], self._socket_source())
        return "; ".join(f"{name} {' '.join(sources)}" for name, sources in directives.items())

    def _response(
        self, status: HTTPStatus, content: str | bytes, content_type: str
    ) -> HttpResponse:
        """Build the single response shape every gateway route returns."""
        from websockets.datastructures import (  # noqa: PLC0415  # lint-waiver: LW-101019 [PLC0415]; defer optional websocket imports until an HTTP response is needed
            Headers,
        )
        from websockets.http11 import (  # noqa: PLC0415  # lint-waiver: LW-101020 [PLC0415]; defer optional websocket imports until an HTTP response is needed
            Response as HttpResponse,
        )

        body = content.encode("utf-8") if isinstance(content, str) else content
        return HttpResponse(
            status.value,
            status.phrase,
            # One dict literal, so no header can be written twice.
            Headers(
                {
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                    **_STATIC_HEADERS,
                    "Content-Security-Policy": self._content_security_policy(),
                }
            ),
            body,
        )

    def _asset_response(self, relative: str, *, subdirectory: str = "") -> HttpResponse:
        """Serve `relative` from `assets_dir/subdirectory`, guarded to that root.

        The guard is rooted at the subtree the URL names, not at `assets_dir`,
        because `/assets/*` is the only token-free URL space: rooting it higher
        would serve a symlink inside the bundle's `assets/` directory that
        points elsewhere under the dist root without a capability token. The
        default root is `assets_dir` itself, for the token-required index.
        """
        if self.assets_dir is None:
            return self._response(
                HTTPStatus.NOT_FOUND, "Web assets are not installed\n", "text/plain"
            )
        try:
            # The root is resolved too, so a deliberately symlinked `assets/`
            # directory still serves while a symlink *inside* it that leaves
            # it does not.
            root = (self.assets_dir / subdirectory).resolve()
            candidate = (root / relative).resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            # `resolve` rejects a NUL byte and an unresolvable symlink chain;
            # `relative_to` rejects a symlink inside the root that escapes it.
            # Both mean the target is not a bundle file.
            return self._response(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain")
        if not candidate.is_file():
            return self._response(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain")
        try:
            body = candidate.read_bytes()
        except OSError:
            return self._response(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to read asset\n", "text/plain"
            )
        return self._response(HTTPStatus.OK, body, _content_type(candidate))

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
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-101016 [BLE001]; a disconnected browser is normal at the transport boundary
            # The websocket library owns close frames. A peer disappearing
            # while the API is writing is therefore a normal stream teardown.
            with suppress(Exception):
                await websocket.close()
            del error

    async def _handle_request(self, websocket: ServerConnection, raw: str) -> bool:
        request_id, client_id = _request_metadata(raw)
        try:
            request = _REQUEST_ADAPTER.validate_json(raw)
            if isinstance(request, SubscribeRequest):
                with self.subscriptions.track():
                    await self._stream(websocket, request)
                return True
            response = self.api.execute(request)
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-101017 [BLE001]; convert malformed browser frames into protocol responses
            response = Response.from_exception(
                request_id,
                error,
                operation="Request",
            ).model_copy(update={"client_id": client_id})
        await websocket.send(response.model_dump_json())
        return False

    async def _stream(self, websocket: ServerConnection, request: SubscribeRequest) -> None:
        try:
            bootstrap = self.api.subscription_bootstrap(
                request.after_sequence, request.tail, store_id=request.store_id
            )
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-101018 [BLE001]; convert API failures into protocol responses at the transport boundary
            await websocket.send(
                ProtocolErrorMessage.from_exception(
                    error,
                    operation="Event stream",
                    code="stream_failed",
                    request_id=request.request_id,
                )
                .model_copy(update={"client_id": request.client_id})
                .model_dump_json()
            )
            return
        await websocket.send(
            SubscribedMessage(
                request_id=request.request_id,
                client_id=request.client_id,
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


def browser_origin(value: str) -> str:
    """Return `value` as the origin a browser would send, or reject it by name.

    The WebSocket handshake compares the `Origin` request header to
    `WebSocketGateway.allowed_origins` by exact string, so an entry a browser
    cannot send is dead configuration that fails silently rather than loudly:
    `http://LOCALHOST:5173` and `http://proxy.example:80` never match what a
    browser sends, `http://[::1` is not a URL, and `http://*:5173` is not an
    authority. Canonicalizing once here leaves one spelling inside the
    gateway: lowercase scheme and host, IPv6 literal compressed and
    bracketed, default port omitted.

    Raises `ValueError`, naming `value` and the reason, otherwise.
    """
    rejection = _origin_rejection(value)
    if rejection is not None:
        raise ValueError(f"{value!r} is not an origin a browser can send: {rejection}")  # noqa: TRY003  # lint-waiver: LW-101107 [TRY003]; name the rejected origin and the reason at the configuration boundary, which a bare exception class cannot do
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    # `hostname` strips an IPv6 literal's brackets, which the serialized origin
    # needs back, and leaves whatever spelling was typed. A browser sends the
    # compressed form, so `http://[0:0:0:0:0:0:0:1]:5173` would otherwise be an
    # entry that never matches. Only a literal can hold a `:` once
    # `_origin_rejection` has restricted every other host to
    # `_HOST_CHARACTERS`, and `urlsplit` has already accepted it as one.
    authority = f"[{ip_address(host).compressed}]" if ":" in host else host
    port = parsed.port
    if port is not None and port != _ORIGIN_DEFAULT_PORTS[parsed.scheme]:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def _origin_rejection(value: str) -> str | None:
    """Return why `value` is not an origin a browser can send, or `None`."""
    try:
        parsed = urlsplit(value)
        # `urlsplit` defers port validation to this attribute, so reading it
        # here is what rejects a non-numeric or out-of-range port.
        port = parsed.port
    except ValueError as error:
        # A malformed IPv6 literal fails `urlsplit` and a bad port fails
        # `port`; either way this is not a URL with an authority.
        return str(error)
    host = parsed.hostname or ""
    # Stated as a table rather than a return ladder, so what an origin *is*
    # reads as one list. A `:` survives in `host` only for an IPv6 literal,
    # which `urlsplit` has already validated, so the character set applies to
    # every other host.
    requirements = (
        (parsed.scheme in _ORIGIN_DEFAULT_PORTS, "the scheme must be http or https"),
        (parsed.username is None and parsed.password is None, "an origin carries no userinfo"),
        (
            parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
            "an origin carries no path, query, or fragment",
        ),
        (bool(host), "the authority names no host"),
        (
            ":" in host or not set(host) - _HOST_CHARACTERS,
            "the host holds only ASCII letters, digits, hyphens, and dots",
        ),
        (port != 0, "the port must be between 1 and 65535"),
    )
    return next((reason for satisfied, reason in requirements if not satisfied), None)


def _request_id(raw: str) -> str:
    return _request_metadata(raw)[0]


def _request_metadata(raw: str) -> tuple[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return "unknown", ""
    if not isinstance(value, dict):
        return "unknown", ""
    client_id = value.get("client_id")
    return str(value.get("request_id", "unknown")), client_id if isinstance(client_id, str) else ""


def _request_header(request: Request, name: str) -> str | None:
    headers = getattr(request, "headers", {})
    return headers.get(name)


def _routing_path(raw_path: str) -> str:
    """Return the request path percent-decoded and lexically normalized.

    The result is rooted at exactly one `/` and holds no `.` or `..` segment,
    so one prefix test on it decides both the capability-token requirement and
    the asset lookup, for the same target.
    """
    return posixpath.normpath("/" + unquote(raw_path).lstrip("/"))


def _content_type(path: Path) -> str:
    known_type = {
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
    }.get(path.suffix.lower())
    if known_type is not None:
        return known_type
    guessed_type, _encoding = mimetypes.guess_type(path.name)
    return guessed_type or "application/octet-stream"


def _connection_closed(connection: object) -> bool:
    state = getattr(connection, "state", None)
    return state == "CLOSED" or getattr(state, "name", None) == "CLOSED"
