"""Loopback WebSocket gateway for browser presentation clients."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import posixpath
import secrets
import threading
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
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
from server.transport.discovery import (
    CAPABILITY_ROTATION_HEADER,
    CAPABILITY_ROTATION_PATH,
    WebInstanceClaim,
    WebInstanceRecord,
)
from server.transport.subscriptions import SubscriptionTracker

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request
    from websockets.http11 import Response as HttpResponse

    from server.api.service import RunApi, SubscriptionBootstrap

_LOG = logging.getLogger(__name__)

_REQUEST_ADAPTER = TypeAdapter(ProtocolRequest)
_DISCONNECT_POLL_SECONDS = 0.1
_LOOPBACK_HOST = "127.0.0.1"
# This gateway's own authority, spelled once. The page origin it accepts and
# the socket source its policy names are this single `host:port` under two
# schemes, so the two cannot come to name different authorities.
_AUTHORITY_TEMPLATE = f"{_LOOPBACK_HOST}:{{port}}"
_WEB_SOCKET_PATH = "/ws"
_HEALTH_PATH = "/health"
_BROWSER_SESSION_HEADER = "X-VibeSys-Browser-Session"
_INDEX_FILE = "index.html"
_INDEX_PATHS = frozenset({"/", f"/{_INDEX_FILE}"})
_ASSET_PREFIX = "/assets/"
# The one subdirectory of the bundle the `/assets/` URL space names, spelled
# once so the prefix test and the filesystem guard cannot disagree.
_ASSET_DIRECTORY = _ASSET_PREFIX.strip("/")
# Every target outside `/assets/` this gateway has a route for, spelled once so
# the "could this ever be served?" test and the routes that answer it cannot
# come to disagree. Membership is what the capability token gates; a target
# outside this set and outside `/assets/` is answered 404 whether or not it
# carried a token, because there is no resource there for a token to unlock.
_ROUTED_PATHS = frozenset({_WEB_SOCKET_PATH, _HEALTH_PATH, CAPABILITY_ROTATION_PATH, *_INDEX_PATHS})

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
# `no-referrer` because the first page request carries the launch capability in
# its query string and a navigation before the client scrubs it could otherwise
# copy it into a `Referer` header. `nosniff` is the policy's companion: `_content_type` falls back
# to `application/octet-stream`, and `/assets/*` is token-free, so no response
# may be re-typed by content sniffing into something the policy would execute.
_STATIC_HEADERS = MappingProxyType(
    {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
)
# One keepalive period, the single number the liveness defaults are built from.
_KEEPALIVE_SECONDS = 20.0
# One frame cap, the single number both frame-size defaults are built from: it
# is the `max_size` default `websockets` applies to received messages, which
# this gateway adopts as its own inbound cap and assumes of a peer. Stated here
# so a dependency bump cannot move either direction;
# `test_the_frame_caps_default_to_the_websockets_value_they_state` fails if the
# installed library's default no longer agrees with it.
_FRAME_BYTES = 1024 * 1024


@dataclass(frozen=True)
class WebSocketLimits:
    """The gateway's liveness and flow-control bounds, stated rather than inherited.

    Every field here was previously either a literal in the ``serve()`` call or
    an unstated ``websockets`` default. They are declared together because they
    compose into the two contracts a subscriber depends on, both written down
    under ``WP-DISCONNECT`` in ``docs/contributing/wire-protocol.md``:

    Liveness. A peer that stops answering is closed after
    ``ping_interval_seconds + ping_timeout_seconds``, and its socket is aborted
    a further ``close_timeout_seconds`` later when the peer never echoes the
    close frame. Only that abort moves the connection to ``CLOSED``, which is
    what ``_stream`` polls for, so the subscription is released one
    ``_DISCONNECT_POLL_SECONDS`` after the sum of all three.

    Flow control. ``send_buffer_bytes`` is the transport high-water mark that
    makes an unread frame stall the producing coroutine instead of queueing
    without bound; it is the WebSocket equivalent of the blocking
    ``wfile.write`` plus ``flush`` in ``unix_jsonl.py``'s ``_write_message``.
    It is unrelated to ``receive_queue``, which bounds frames arriving *from*
    the peer. ``write_deadline_seconds`` bounds how long that stall may last
    before the peer is treated as gone; without it a peer that neither reads
    nor answers pings also blocks the library's own keepalive writes, and no
    liveness bound holds at all.

    Frame size. ``receive_frame_bytes`` and ``send_frame_bytes`` are the hard
    caps, and unlike the flow-control pair they are caps rather than
    thresholds: exceeding one ends the connection with close code 1009 instead
    of stalling a writer. They are separate fields holding the same number
    because they are bounds on different things, only one of which this process
    enforces. ``receive_frame_bytes`` is passed to ``serve()`` as ``max_size``
    and is what this gateway refuses to receive. ``send_frame_bytes`` is a
    statement about *peers*: ``max_size`` applies to received messages on each
    side, so naming it here cannot change what a peer accepts, and the send
    side is bounded by chunking instead (``_event_batch_chunks``). Its value is
    therefore chosen against the smallest cap a supported client imposes, not
    against this gateway's own: browsers impose no receive limit, so the
    binding client is the Python ``websockets`` client, whose default is the
    same 1 MiB.

    Defaults reproduce the values in force before they were named, so the
    library's own defaults can no longer move them. The field defaults below
    are the only statement of those numbers in this module; the published
    liveness table in ``wire-protocol.md`` quotes them, and
    ``test_the_stated_transport_bounds_are_the_values_they_replaced`` asserts
    the whole tuple so an edit here cannot silently falsify the table. The
    default write deadline is one full keepalive reaping window, because a
    write that cannot make progress for as long as an unresponsive peer would
    take to fail its keepalive is the same failure and deserves the same bound.

    The fields are independent rather than derived from each other so a test
    can exercise one mechanism at a time: raising the ping interval takes the
    keepalive out of the picture while leaving the write deadline in it, and
    vice versa. Deriving the deadline instead made the two race in any test
    small enough to run in CI. Lowering ``send_frame_bytes`` alone is what lets
    a test exercise chunking without building a megabyte of events.
    """

    ping_interval_seconds: float = _KEEPALIVE_SECONDS
    ping_timeout_seconds: float = _KEEPALIVE_SECONDS
    close_timeout_seconds: float = 10.0
    write_deadline_seconds: float = 2 * _KEEPALIVE_SECONDS
    send_buffer_bytes: int = 32768
    receive_queue: tuple[int, int] = (32, 8)
    receive_frame_bytes: int = _FRAME_BYTES
    send_frame_bytes: int = _FRAME_BYTES


class WebSocketGateway:
    """Serve the existing protocol API and web assets on one loopback port.

    WebSocket connections retain the Unix transport's role model: ordinary
    requests are handled serially on one connection, subscriptions take over
    their connection, and chat is free to occupy a dedicated connection. The
    gateway only changes framing, from JSONL lines to one text frame per
    protocol message. The API and subscription tracker remain shared with the
    Unix adapter.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-101057 [PLR0913]; the gateway exposes independent protocol, asset, port, capability, discovery, and transport-bound options
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
        limits: WebSocketLimits | None = None,
    ) -> None:
        """Create a loopback gateway around a shared run API.

        Raises `ValueError`, naming the offending value, when `allowed_origins`
        holds anything a browser cannot send as an `Origin` header.
        """
        self.api = api
        self.assets_dir = assets_dir.resolve() if assets_dir is not None else None
        self.port = port
        self._token = token or secrets.token_urlsafe(32)
        # The launch capability is intentionally distinct from the credential
        # a browser receives after presenting it. Rotating the former can then
        # invalidate copied launch URLs without disconnecting an attached page
        # or breaking that page's later control and subscription sockets.
        self._browser_session_key = secrets.token_bytes(32)
        self._credential_lock = threading.Lock()
        self.instance_path = instance_path
        self.project_root = project_root or Path.cwd()
        # Parsed here because this is the one boundary declared origins enter
        # through, so the stored set holds the exact spelling a browser sends
        # and an origin no browser can send is a construction-time error rather
        # than an allowlist entry that silently never matches.
        self.allowed_origins = frozenset(browser_origin(origin) for origin in allowed_origins)
        self.limits = limits or WebSocketLimits()
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
    def token(self) -> str:
        """Return the current launch capability."""
        with self._credential_lock:
            return self._token

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

    def rotate_capability(self, *, expected_token: str | None = None) -> str:
        """Replace the launch capability without disturbing browser sessions.

        ``expected_token`` makes an HTTP rotation request compare and replace
        atomically. A stale lifecycle command cannot rotate a newer record.
        """
        with self._credential_lock:
            if expected_token is not None and not _same_secret(expected_token, self._token):
                raise PermissionError
            old_token = self._token
            new_token = secrets.token_urlsafe(32)
            while _same_secret(new_token, old_token):  # pragma: no cover - cryptographic collision
                new_token = secrets.token_urlsafe(32)
            record = self._instance_record
            replacement = record.with_token(new_token) if record is not None else None
            self._token = new_token
            try:
                if replacement is not None and self.instance_path is not None:
                    replacement.write(self.instance_path)
            except OSError:
                self._token = old_token
                raise
            self._instance_record = replacement
            return new_token

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
            # Every bound below is stated, never inherited: see
            # ``WebSocketLimits``. ``write_limit`` is the send-side high-water
            # mark that makes an unread frame stall the producer, the analogue
            # of ``unix_jsonl.py``'s blocking ``wfile.write``; ``max_queue``
            # bounds the opposite direction, frames arriving from the peer, as
            # does ``max_size``: it is the cap on what this gateway will
            # *receive*, and says nothing about what a peer will accept from
            # it. The send side is bounded by ``_event_batch_chunks`` instead.
            ping_interval=self.limits.ping_interval_seconds,
            ping_timeout=self.limits.ping_timeout_seconds,
            close_timeout=self.limits.close_timeout_seconds,
            write_limit=self.limits.send_buffer_bytes,
            max_queue=self.limits.receive_queue,
            max_size=self.limits.receive_frame_bytes,
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

    async def _process_request(  # noqa: C901, PLR0911  # lint-waiver: LW-101058 [C901, PLR0911]; each HTTP route returns its precise status and body at this protocol boundary
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
        if not serves_asset and path not in _ROUTED_PATHS:
            # "Is this a path we could ever serve?" is decided before "is the
            # token valid?", because the first question is about this module's
            # route table and the second is about the caller's credential.
            # Folding an unrouted target into the token branch spent 403, the
            # one signal an operator reads as "the capability token is wrong",
            # on a request no token could ever have satisfied: a browser probes
            # `/favicon.ico` on its own for every document it loads, and that
            # probe carries no query string, so every page load logged a
            # permanent 403 unrelated to the token.
            #
            # This does make the route table observable without a token, since
            # a routed target answers 403 where an unrouted one answers 404.
            # The set is these module constants plus `/assets/*`, which is
            # already served token-free, so nothing per-run or per-deployment
            # is disclosed, and an unrouted target now answers alike with and
            # without a token instead of reporting token validity for a
            # resource that does not exist.
            return self._response(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain")
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
        explicit_capability = "token" in query
        capability_valid = self._matches_capability(token)
        browser_session_valid = not explicit_capability and (
            self._matches_browser_session(request)
            or self._valid_browser_session_token(query.get("session", [""])[0])
        )
        session_route = path == _WEB_SOCKET_PATH or path in _INDEX_PATHS
        authorized = (
            capability_valid if explicit_capability else browser_session_valid and session_route
        )
        if not serves_asset and not authorized:
            return self._response(
                HTTPStatus.FORBIDDEN, "Invalid VibeSys capability token\n", "text/plain"
            )

        if path == CAPABILITY_ROTATION_PATH:
            if _request_header(request, CAPABILITY_ROTATION_HEADER) != "1":
                return self._response(
                    HTTPStatus.BAD_REQUEST,
                    f"Missing {CAPABILITY_ROTATION_HEADER} header\n",
                    "text/plain",
                )
            try:
                self.rotate_capability(expected_token=token)
            except PermissionError:
                return self._response(
                    HTTPStatus.FORBIDDEN, "Stale VibeSys capability token\n", "text/plain"
                )
            except OSError as error:
                _LOG.warning("unable to publish rotated web capability: %s", error)
                return self._response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "Unable to publish rotated VibeSys capability\n",
                    "text/plain",
                )
            return self._response(HTTPStatus.OK, "vibesys-capability-rotated\n", "text/plain")

        if path == _WEB_SOCKET_PATH:
            origin = _request_header(request, "Origin")
            if origin not in self._allowed_origins():
                return self._response(
                    HTTPStatus.FORBIDDEN, "Invalid WebSocket origin\n", "text/plain"
                )
            return None

        if path == _HEALTH_PATH:
            return self._response(HTTPStatus.OK, "vibesys-ok\n", "text/plain")

        if path in _INDEX_PATHS:
            headers = self._browser_session_headers(request) if capability_valid else None
            return self._asset_response(_INDEX_FILE, response_headers=headers)
        return self._asset_response(path.removeprefix(_ASSET_PREFIX), subdirectory=_ASSET_DIRECTORY)

    def _matches_capability(self, candidate: str) -> bool:
        with self._credential_lock:
            return _same_secret(candidate, self._token)

    def _browser_cookie_name(self) -> str:
        port = self.port if self._bound_port is None else self._bound_port
        return f"vibesys_gateway_{port}"

    def _matches_browser_session(self, request: Request) -> bool:
        raw = _request_header(request, "Cookie")
        if raw is None:
            return False
        cookies = SimpleCookie()
        try:
            cookies.load(raw)
        except CookieError:
            return False
        morsel = cookies.get(self._browser_cookie_name())
        if morsel is None:
            return False
        return self._valid_browser_session_token(morsel.value)

    def _valid_browser_session_token(self, token: str) -> bool:
        nonce, separator, signature = token.rpartition(".")
        if not separator or not nonce or not signature:
            return False
        expected = hmac.new(
            self._browser_session_key,
            nonce.encode("utf-8", "surrogateescape"),
            hashlib.sha256,
        ).hexdigest()
        return _same_secret(signature, expected)

    def _browser_session_headers(self, request: Request) -> Mapping[str, str]:
        nonce = secrets.token_urlsafe(32)
        signature = hmac.new(
            self._browser_session_key,
            nonce.encode(),
            hashlib.sha256,
        ).hexdigest()
        browser_session_token = f"{nonce}.{signature}"
        headers = {
            _BROWSER_SESSION_HEADER: browser_session_token,
            "Set-Cookie": (
                f"{self._browser_cookie_name()}={browser_session_token}; "
                "Path=/; HttpOnly; SameSite=Strict"
            ),
        }
        origin = _request_header(request, "Origin")
        if origin in self._allowed_origins():
            headers.update(
                {
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Expose-Headers": _BROWSER_SESSION_HEADER,
                    "Vary": "Origin",
                }
            )
        return headers

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
        self,
        status: HTTPStatus,
        content: str | bytes,
        content_type: str,
        *,
        response_headers: Mapping[str, str] | None = None,
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
                    **(response_headers or {}),
                }
            ),
            body,
        )

    def _asset_response(
        self,
        relative: str,
        *,
        subdirectory: str = "",
        response_headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
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
        return self._response(
            HTTPStatus.OK,
            body,
            _content_type(candidate),
            response_headers=response_headers,
        )

    async def _send(self, websocket: ServerConnection, payload: str) -> None:
        """Write one protocol frame, stalling at most one write deadline.

        Every *protocol* frame the gateway emits goes through here, so the
        send-side policy lives in one place: stall the producing coroutine
        while the peer is behind (which is what lets the next
        ``subscription_checkpoint`` coalesce a backlog into one batch), and
        abandon the peer once the stall outlasts ``write_deadline_seconds``.
        The one write that does not is the library-owned close frame in
        ``_handle_connection``'s teardown, whose ``drain()`` is unbounded;
        that path runs only after the stream is already ending.

        The deadline is what makes the liveness bound true rather than
        conditional. ``websockets`` ends every write, including its own
        keepalive ping and close frames, in ``drain()``, which suspends while
        the transport sits above ``send_buffer_bytes``. A peer that neither
        drains its socket nor answers pings therefore blocks the keepalive that
        was supposed to reap it, and without this deadline the subscription is
        never released and a non-detached run never exits. The keepalive's own
        ping write is still not covered by any deadline, so the keepalive bound
        holds only while the transport dips below its low-water mark, which it
        does whenever a ``send`` here returns.

        The transport is aborted rather than closed because a close frame is
        itself a write: it would re-enter the same stalled ``drain()``. This is
        the library's own escape hatch for the same problem. ``Connection``'s
        ``send_context`` ends in one ``transport.abort()``
        (``websockets.asyncio.connection``, 14.2 line 931) shared by four
        ``raise_close_exc`` assignments, of which the close-deadline one at
        line 925 is the analogue here: a peer that will not complete the
        closing handshake is abandoned rather than waited on. The four aborts
        in ``Server.conn_handler`` are not this case; they are cancellation
        during the opening handshake, handshake failure, a rejected handshake,
        and an unexpected error.
        """
        try:
            async with asyncio.timeout(self.limits.write_deadline_seconds):
                await websocket.send(payload)
        except TimeoutError:
            # Reported here, where the decision is made, rather than in
            # ``_handle_connection``'s handler: that handler also absorbs a
            # browser closing its tab, so it cannot say anything above debug
            # without either crying wolf or type-sniffing its own exception.
            _LOG.warning(
                "abandoning a websocket peer that did not drain within %ss",
                self.limits.write_deadline_seconds,
            )
            websocket.transport.abort()
            raise

    async def _handle_connection(self, connection: ServerConnection) -> None:
        websocket = connection
        try:
            async for raw in websocket:
                if not isinstance(raw, str):
                    await self._send(
                        websocket,
                        ProtocolErrorMessage(
                            code="invalid_frame",
                            message="WebSocket frames must contain text",
                        ).model_dump_json(),
                    )
                    continue
                if await self._handle_request(websocket, raw):
                    return
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-101016 [BLE001]; a disconnected browser is normal at the transport boundary
            # The websocket library owns close frames, so a peer disappearing
            # while the API is writing is a normal stream teardown. Debug
            # rather than silence, because this clause is also where the
            # re-raised write deadline lands, and `del error` made the two
            # indistinguishable; the deadline itself is reported at warning by
            # ``_send``, which is what decided to abandon the peer.
            _LOG.debug("websocket stream ended: %r", error)
            with suppress(Exception):
                await websocket.close()

    async def _handle_request(self, websocket: ServerConnection, raw: str) -> bool:
        request_id, client_id = _request_metadata(raw)
        try:
            request = _REQUEST_ADAPTER.validate_json(raw)
            if isinstance(request, SubscribeRequest):
                with self.subscriptions.track():
                    await self._stream(websocket, request)
                return True
            response = self.api.execute(request)
        except TimeoutError:
            # A write deadline is not a request failure, and ``TimeoutError``
            # is an ``OSError`` subclass, so without this the broad handler
            # below would convert it into a control-path ``Response`` and hand
            # it to ``_send`` for the transport that was just aborted: a second
            # doomed write, on the wrong envelope, costing a second deadline.
            raise
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-101017 [BLE001]; convert malformed browser frames into protocol responses
            response = Response.from_exception(
                request_id,
                error,
                operation="Request",
            ).model_copy(update={"client_id": client_id})
        await self._send(websocket, response.model_dump_json())
        return False

    async def _stream(self, websocket: ServerConnection, request: SubscribeRequest) -> None:
        try:
            bootstrap = self.api.subscription_bootstrap(
                request.after_sequence, request.tail, store_id=request.store_id
            )
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-101018 [BLE001]; convert API failures into protocol responses at the transport boundary
            await self._send(
                websocket,
                ProtocolErrorMessage.from_exception(
                    error,
                    operation="Event stream",
                    code="stream_failed",
                    request_id=request.request_id,
                )
                .model_copy(update={"client_id": request.client_id})
                .model_dump_json(),
            )
            return
        await self._send(
            websocket,
            SubscribedMessage(
                request_id=request.request_id,
                client_id=request.client_id,
                run_id=bootstrap.run_id,
                latest_sequence=bootstrap.through_sequence,
            ).model_dump_json(),
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
            await self._send_event_batch(
                websocket,
                EventBatchMessage(
                    events=checkpoint.events,
                    through_sequence=checkpoint.through_sequence,
                    active_executions=checkpoint.active_executions,
                    history_after_sequence=reported_floor,
                    store_id=store_id,
                ),
            )
            cursor = checkpoint.through_sequence

    async def _write_bootstrap(
        self,
        websocket: ServerConnection,
        request: SubscribeRequest,
        bootstrap: SubscriptionBootstrap,
    ) -> tuple[int, int, str]:
        reported_floor = 0 if request.tail is None else bootstrap.floor
        await self._send_event_batch(
            websocket,
            EventBatchMessage(
                events=bootstrap.events,
                through_sequence=bootstrap.through_sequence,
                active_executions=bootstrap.active_executions,
                history_after_sequence=reported_floor,
                store_id=bootstrap.store_id,
            ),
        )
        return bootstrap.through_sequence, reported_floor, bootstrap.store_id

    async def _send_event_batch(
        self, websocket: ServerConnection, batch: EventBatchMessage
    ) -> None:
        """Write *batch* as the fewest frames that each fit ``send_frame_bytes``.

        Every ``event_batch`` the gateway emits goes through here, so the one
        batch whose size is not bounded by anything the client asked for,
        ``_write_bootstrap``'s whole-backlog replay, cannot reach the wire as a
        frame no peer will accept. A client resuming after a long detached run
        supplies ``tail`` only if it has one, so the batch is otherwise sized by
        the run's entire history.
        """
        for chunk in _event_batch_chunks(batch, self.limits.send_frame_bytes):
            await self._send(websocket, chunk.model_dump_json())


def _event_batch_chunks(batch: EventBatchMessage, budget: int) -> Iterator[EventBatchMessage]:
    """Split *batch* into whole batches that each serialize within *budget* bytes.

    Chunking is on serialized size, not on event count. An event count does not
    bound bytes: measured on a run of output events the per-event JSON ranges
    from 282 to 647 bytes, so any count that is safe for the large ones wastes
    most of the frame on the small ones and any count tuned to the average
    overflows on a burst of large ones.

    Each chunk is a complete ``event_batch`` message rather than a fragment of
    one, so no receiver needs a reassembly step and ``WP-GRANULARITY`` still
    holds: one frame is still exactly one protocol message. What makes that
    safe is the ``through_sequence`` on each chunk. It is the sequence of the
    last event that chunk carries, not the batch's, so a consumer that folds
    batches in order advances its cursor exactly as far as the events it has
    actually seen. A connection lost mid-replay therefore resumes from a cursor
    that is true, which is what keeps the client's bootstrap detection correct
    (``persistent-event-stream.ts`` marks itself bootstrapped on the *first*
    ``event_batch`` of a fresh dial and resumes from its cursor afterwards). The
    final chunk keeps the batch's own ``through_sequence``, which may run past
    its last event when the journal's watermark advanced over events the
    checkpoint did not return; carrying it on an earlier chunk would claim
    delivery of events still to come.

    The split is computed from measured byte lengths rather than by serializing
    candidate chunks, which would be quadratic. The arithmetic is exact because
    compact JSON renders a list as its items joined by commas: a chunk costs
    its envelope (measured once, at the batch's own ``through_sequence``, the
    longest number any chunk carries) plus each event's own serialization plus
    one separator between neighbors. That identity is a property of the
    serializer rather than of this function, so it is pinned from the outside:
    ``test_a_chunked_batch_reaches_the_peer_whole_and_within_the_stated_send_cap``
    asserts the real frame bytes over generated batches and fails if it ever
    stops holding.

    The one case *budget* cannot bound is a single event whose own
    serialization exceeds it: a chunk always carries at least one event, so it
    is emitted oversized rather than dropped or truncated. Nothing bounds an
    individual event's size today, so that is a payload-level gap this
    transport cannot close.
    """
    if not batch.events:
        # One frame, so an empty checkpoint still reports its watermark.
        yield batch
        return
    room = budget - len(batch.model_copy(update={"events": []}).model_dump_json().encode())
    # Boundaries are indices into `batch.events` rather than an accumulated
    # list, so the chunks are slices of the batch's own field and this function
    # never needs to name the event type. `server.transport` may not depend on
    # `server.events`, and the alternative was a `tach.toml` edge for a local
    # annotation.
    start = 0
    used = 0
    for index, event in enumerate(batch.events):
        size = len(event.model_dump_json().encode())
        if index > start and used + 1 + size > room:
            yield batch.model_copy(
                update={
                    "events": batch.events[start:index],
                    "through_sequence": batch.events[index - 1].sequence,
                }
            )
            start, used = index, 0
        used += size + (1 if index > start else 0)
    yield batch.model_copy(update={"events": batch.events[start:]})


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


def _same_secret(candidate: str, expected: str) -> bool:
    """Compare request-derived secrets without rejecting non-ASCII bytes."""
    return secrets.compare_digest(
        candidate.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8", "surrogateescape"),
    )


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
