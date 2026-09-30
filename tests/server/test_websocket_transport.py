"""Loopback WebSocket gateway contract tests."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import socket
import struct
import threading
from http.client import HTTPConnection, HTTPMessage, HTTPResponse
from pathlib import Path
from string import ascii_lowercase, digits
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from urllib.request import urlopen

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.server.support import build_server_parts
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.protocol import State

from server.api.protocol import SnapshotQuery, SubscribeRequest
from server.transport.discovery import WebInstanceRecord
from server.transport.subscriptions import SubscriptionTracker
from server.transport.unix_jsonl import UnixJsonlServer
from server.transport.websocket import (
    WebSocketGateway,
    WebSocketLimits,
    _connection_closed,
    _content_type,
    _request_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request
    from websockets.typing import Origin

# A burst has to outrun everything that could absorb it before the producing
# coroutine stalls: the peer's kernel receive buffer, which is capped here, and
# the server's send buffer, which is not settable from this side and which
# Linux autotunes up to ``net.ipv4.tcp_wmem``'s ceiling (4 MiB stock). 8 MiB
# leaves a factor of two over that ceiling. Absorbing the whole burst would
# fail this test rather than pass it vacuously, so the margin is not load
# bearing for correctness, only for not reporting a false regression.
_PEER_RECEIVE_BUFFER_BYTES = 2048
_STALL_EVENT_BYTES = 64 * 1024
_STALL_EVENTS = 128
# RFC 6455's two payload-length escape values.
_EXTENDED_LENGTH = 126
_EXTENDED_LENGTH_64 = 127


def _http_request(path: str) -> Request:
    return cast("Request", SimpleNamespace(path=path, headers={}))


def test_gateway_serves_assets_and_round_trips_protocol_frames(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")
    assets = tmp_path / "web"
    assets.mkdir()
    (assets / "assets").mkdir()
    (assets / "index.html").write_text("<!doctype html><title>VibeSys</title>")
    image = b"\x89PNG\r\n\x1a\n\x00\xff\x80"
    (assets / "assets" / "logo.png").write_bytes(image)

    with WebSocketGateway(parts.api, assets_dir=assets) as gateway:
        with urlopen(gateway.url, timeout=2) as response:  # noqa: S310  # lint-waiver: LW-101021 [S310]; connect only to the loopback URL produced by the gateway under test
            assert response.status == 200
            assert response.read() == b"<!doctype html><title>VibeSys</title>"
        asset_url = f"http://127.0.0.1:{gateway.bound_port}/assets/logo.png"
        with urlopen(asset_url, timeout=2) as response:  # noqa: S310  # lint-waiver: LW-101105 [S310]; connect only to the loopback URL produced by the gateway under test
            assert response.status == 200
            assert response.headers["Content-Type"] == "image/png"
            assert response.read() == image

        response = asyncio.run(_request(gateway, SnapshotQuery()))

    assert response["ok"] is True
    assert response["snapshot"]["status"] == "running"


def _expected_policy(*socket_origins: str) -> str:
    """Return the policy the gateway must serve, with no parser on either side.

    This is the cross-check copy of `_POLICY_DIRECTIVES`, so loosening,
    dropping, or reordering a directive fails here rather than passing a
    per-directive comparison that a hand-rolled CSP parser could mis-split.
    """
    connect = " ".join(("'self'", *socket_origins))
    return (
        "default-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self'; "
        "font-src 'none'; "
        f"connect-src {connect}; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )


def test_gateway_sends_each_hygiene_header_once_on_every_route(tmp_path: Path) -> None:
    with _asset_gateway(tmp_path) as gateway:
        routes = {
            name: _fetch(gateway.bound_port, path)
            for name, path in (
                ("index", f"/?token={gateway.token}"),
                ("asset", "/assets/index.js"),
                ("health", f"/health?token={gateway.token}"),
                ("missing-token", "/"),
                # A token the constant-time comparison cannot take as `str`.
                # Rejected like any other wrong token, and through this
                # response builder, not through the library's handshake error
                # path, which carries none of these headers.
                ("non-ascii-token", "/?token=%C3%A9"),
                ("unknown-route", f"/nope?token={gateway.token}"),
            )
        }
        # The same bytes spelled raw rather than percent-encoded, which
        # `HTTPConnection` cannot express. `websockets` decodes the request
        # target with `ascii`/`surrogateescape`, so these arrive as lone
        # surrogates, which strict UTF-8 refuses just as `compare_digest`
        # refuses a non-ASCII `str`. Both spellings have to answer alike.
        routes.update(
            {
                name: _raw_fetch(gateway.bound_port, target)
                for name, target in (
                    ("raw-non-ascii-token", b"/?token=\xc3\xa9"),
                    ("raw-non-ascii-token-on-health", b"/health?token=\x80"),
                    ("raw-non-ascii-token-on-socket", b"/ws?token=\xff"),
                )
            }
        )
        expected_policy = _expected_policy(f"ws://127.0.0.1:{gateway.bound_port}")

    assert {name: status for name, (status, _headers) in routes.items()} == {
        "index": 200,
        "asset": 200,
        "health": 200,
        "missing-token": 403,
        "non-ascii-token": 403,
        "raw-non-ascii-token": 403,
        "raw-non-ascii-token-on-health": 403,
        "raw-non-ascii-token-on-socket": 403,
        "unknown-route": 404,
    }
    for name, (_status, headers) in routes.items():
        assert headers.get_all("Cache-Control") == ["no-store"], name
        assert headers.get_all("Referrer-Policy") == ["no-referrer"], name
        assert headers.get_all("X-Content-Type-Options") == ["nosniff"], name
        assert headers.get_all("Content-Security-Policy") == [expected_policy], name


# Origins an operator can legitimately declare, spelled every way a browser
# could send one: a generated domain, both schemes in both cases, an IPv6
# literal, a trailing-dot FQDN, and a present or absent port.
_ORIGIN_HOSTS = st.one_of(
    st.lists(
        st.text(alphabet=ascii_lowercase + digits, min_size=1, max_size=6), min_size=1, max_size=3
    ).map(".".join),
    st.sampled_from(["localhost", "127.0.0.1", "[::1]", "[fe80::1]", "localhost.", "a-b.example"]),
)
_DECLARED_ORIGINS = st.builds(
    lambda scheme, host, port: f"{scheme}://{host}" + ("" if port is None else f":{port}"),
    st.sampled_from(["http", "https", "HTTP", "Https"]),
    _ORIGIN_HOSTS,
    st.one_of(st.none(), st.integers(min_value=1, max_value=65535)),
)


@settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
# The documented development origins, pinned so the normal case is always drawn.
@example(
    origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "https://localhost:4173",
        "http://[::1]:5173",
    ]
)
@given(origins=st.lists(_DECLARED_ORIGINS, min_size=1, max_size=4))
def test_gateway_names_only_its_own_socket_authority_whatever_origins_are_declared(
    tmp_path: Path, origins: list[str]
) -> None:
    with _asset_gateway(tmp_path, allowed_origins=origins) as gateway:
        status, headers = _fetch(gateway.bound_port, f"/?token={gateway.token}")
        expected_policy = _expected_policy(f"ws://127.0.0.1:{gateway.bound_port}")

    # Asserted, not discarded: on a 403 the header table below would still
    # match, so without this the test passes on a route that never served.
    assert status == 200
    # The whole served policy is invariant in the declared origins, which is
    # the property the single-example assertion cannot give: `connect-src`
    # answers where a *gateway-served* page may connect, and every page this
    # gateway serves is at its own authority. Because no declared string
    # reaches the header, a wildcard host (`http://*:5173`), an embedded `;`
    # that would split the directive, a space, or a non-ASCII byte cannot
    # appear in it, whatever an operator declares.
    assert headers.get_all("Content-Security-Policy") == [expected_policy]


_REJECTED_ORIGINS = st.one_of(
    st.builds(
        lambda host: f"http://{host}:5173",
        st.sampled_from(
            [
                # A bare wildcard is a valid CSP `host-part`, so the previous
                # rule put `ws://*:<port>` in the header and authorized the
                # token to any host at this gateway's port.
                "*",
                "*.example.test",
                # A `;` terminates the directive, so the rest became a junk
                # directive and `connect-src` lost its own socket source.
                "!;evil.example",
                "x y",
                "exämple.test",
                "user:pw@host",
                "[::1",
                "[]",
                "[:::]",
                "",
            ]
        ),
    ),
    st.sampled_from(
        [
            "file:///tmp/page.html",
            "ws://localhost:5173",
            "http://localhost:5173/app",
            "http://localhost:5173/?token=leaked",
            "http://localhost:5173#fragment",
            "http://localhost:0",
            "http://localhost:99999",
            "http://localhost:not-a-port",
            "//localhost:5173",
            "",
        ]
    ),
)


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(origin=_REJECTED_ORIGINS)
def test_gateway_rejects_a_declared_origin_no_browser_can_send(tmp_path: Path, origin: str) -> None:
    parts = build_server_parts(tmp_path / "logs")

    # Rejected while constructing, not per response: parsing these lazily made
    # an unparseable origin a 500 on every route instead of a startup error,
    # and an origin no browser can send is dead configuration either way.
    with pytest.raises(ValueError, match="is not an origin a browser can send") as failure:
        WebSocketGateway(parts.api, allowed_origins=(origin,))

    # The operator has to be able to find the value they typed.
    assert repr(origin) in str(failure.value)


def test_gateway_accepts_the_origin_a_browser_sends_for_a_differently_spelled_declaration(
    tmp_path: Path,
) -> None:
    parts = build_server_parts(tmp_path / "logs")
    # What an operator may type, against what a browser actually sends for it:
    # the scheme and host arrive lowercased, a default port is omitted, and an
    # IPv6 literal arrives compressed. The handshake compares `Origin` by exact
    # string, so an unnormalized entry is an allowlist entry that can never
    # match.
    declared = (
        "http://LOCALHOST:5173",
        "https://Proxy.Example:443",
        "HTTP://127.0.0.1:80",
        "http://[0:0:0:0:0:0:0:1]:5173",
    )

    with WebSocketGateway(parts.api, allowed_origins=declared) as gateway:
        assert gateway.allowed_origins == {
            "http://localhost:5173",
            "https://proxy.example",
            "http://127.0.0.1",
            "http://[::1]:5173",
        }
        response = asyncio.run(_request(gateway, SnapshotQuery(), origin="http://localhost:5173"))

    assert response["ok"] is True


_INDEX_BODY = b"<!doctype html><title>VibeSys</title>"
_BUNDLE_BODY = b"export {};\n"


def _asset_gateway(tmp_path: Path, *, allowed_origins: Sequence[str] = ()) -> WebSocketGateway:
    parts = build_server_parts(tmp_path / "logs")
    assets = tmp_path / "web"
    (assets / "assets").mkdir(parents=True, exist_ok=True)
    (assets / "index.html").write_bytes(_INDEX_BODY)
    (assets / "assets" / "index.js").write_bytes(_BUNDLE_BODY)
    # Lives at the assets root, so only a traversal out of `/assets/` reaches it.
    (assets / "operator-notes.txt").write_text("not part of the bundle\n")
    return WebSocketGateway(parts.api, assets_dir=assets, allowed_origins=allowed_origins)


# Routing one normalized target makes trailing slashes and `.`/`..` segments
# equivalent to their normalized form. Pinned because it is a deliberate
# behavior change: at the merge base every entry below except `/health`, `/`,
# `/index.html`, and `/ws` was a 404.
_NORMALIZED_ROUTES = {
    "/health": 200,
    "/health/": 200,
    "/health//": 200,
    "/index.html": 200,
    "/index.html/": 200,
    "/./index.html": 200,
    "/index.html/./": 200,
    "/": 200,
    "/..": 200,
    "/../..": 200,
    "/./": 200,
    # `/ws` and its trailing-slash spellings now enter the WebSocket route,
    # which rejects a plain GET whose `Origin` does not match. Accepted on
    # purpose: the Origin and token checks still apply to every spelling, and
    # normalizing the target exactly once is what makes the `/assets/`
    # exemption mean what the decision record says it means.
    "/ws": 403,
    "/ws/": 403,
    "/ws//": 403,
}


def test_gateway_routes_dot_segments_and_trailing_slashes_as_the_normalized_target(
    tmp_path: Path,
) -> None:
    with _asset_gateway(tmp_path) as gateway:
        statuses = {
            path: _fetch(gateway.bound_port, f"{path}?token={gateway.token}")[0]
            for path in _NORMALIZED_ROUTES
        }
        above_root = _fetch_body(gateway.bound_port, f"/..?token={gateway.token}")

    assert statuses == _NORMALIZED_ROUTES
    # A target above the root normalizes to the root rather than escaping it.
    assert above_root == (200, _INDEX_BODY)


_TRAVERSAL_PATHS = (
    "/assets/index.js",
    # A trailing slash or `.` on a file normalizes away, so these are the same
    # target and stay token-free. Pinned because it follows from the accepted
    # normalization rather than from an explicit route.
    "/assets/index.js/",
    "/assets/index.js/.",
    # One segment up leaves `/assets/` entirely, so the token applies again.
    "/assets/index.js/..",
    "/assets/../index.html",
    "/assets/%2e%2e/index.html",
    "/assets/../operator-notes.txt",
    "/assets/%2e%2e/operator-notes.txt",
    "/assets/%2e%2e/%2e%2e/etc/passwd",
)


def test_gateway_requires_the_token_for_paths_reachable_only_by_traversal(tmp_path: Path) -> None:
    with _asset_gateway(tmp_path) as gateway:
        tokenless = {path: _fetch(gateway.bound_port, path)[0] for path in _TRAVERSAL_PATHS}
        with_token = {
            path: _fetch(gateway.bound_port, f"{path}?token={gateway.token}")[0]
            for path in _TRAVERSAL_PATHS
        }

    assert tokenless == {
        # The one genuine bundle request stays token-free.
        "/assets/index.js": 200,
        "/assets/index.js/": 200,
        "/assets/index.js/.": 200,
        "/assets/index.js/..": 403,
        # Everything a traversal reaches leaves `/assets/`, so the token applies.
        "/assets/../index.html": 403,
        "/assets/%2e%2e/index.html": 403,
        "/assets/../operator-notes.txt": 403,
        "/assets/%2e%2e/operator-notes.txt": 403,
        "/assets/%2e%2e/%2e%2e/etc/passwd": 403,
    }
    # With the token the same paths route as their normalized target, which is
    # never a served file: no traversal reaches the assets root either way.
    assert with_token == {
        "/assets/index.js": 200,
        "/assets/index.js/": 200,
        "/assets/index.js/.": 200,
        # Normalizes to `/assets`, which is not under `/assets/`.
        "/assets/index.js/..": 404,
        "/assets/../index.html": 200,
        "/assets/%2e%2e/index.html": 200,
        "/assets/../operator-notes.txt": 404,
        "/assets/%2e%2e/operator-notes.txt": 404,
        "/assets/%2e%2e/%2e%2e/etc/passwd": 404,
    }


def test_gateway_rejects_an_asset_target_the_filesystem_cannot_name(tmp_path: Path) -> None:
    targets = ("/assets/%00", "/assets/%00/index.js", "/assets/in%00dex.js")
    with _asset_gateway(tmp_path) as gateway:
        statuses = {path: _fetch(gateway.bound_port, path)[0] for path in targets}

    # `resolve` raises `ValueError` on an embedded NUL instead of returning a
    # path, which left the handshake as an unauthenticated 500 before the
    # lookup caught it. The property test below generates `%00` too; this pins
    # it without depending on which examples Hypothesis draws.
    assert statuses == dict.fromkeys(targets, 404)


def test_gateway_does_not_serve_a_symlink_out_of_the_token_free_subtree(tmp_path: Path) -> None:
    gateway = _asset_gateway(tmp_path)
    assert gateway.assets_dir is not None
    served = gateway.assets_dir / "assets" / "sub"
    served.mkdir()
    served.joinpath("notes").symlink_to(Path("../../operator-notes.txt"))

    with gateway:
        escaping = _fetch_body(gateway.bound_port, "/assets/sub/notes")
        inside = _fetch_body(gateway.bound_port, "/assets/index.js")

    # `/assets/*` is the only token-free URL space, so the filesystem guard has
    # to be rooted at the subtree the URL names and not at the dist root above
    # it: otherwise a link planted under `assets/` serves a non-bundle file
    # with no capability token.
    assert escaping == (404, b"Not found\n")
    assert inside == (200, _BUNDLE_BODY)


# Every encoding of a traversal the normalizer has to collapse or reject:
# literal and single-encoded `..`, an encoded separator inside a segment, a
# double-encoded `..` (which must survive as a literal segment name, because
# decoding happens exactly once), and a NUL byte, which `resolve` rejects.
_TRAVERSAL_SEGMENTS = st.sampled_from(
    [
        "..",
        "%2e%2e",
        "%2E%2E",
        ".",
        "%2e",
        ".%2e",
        "..%2f",
        "%2e%2e%2f",
        "%252e%252e",
        "%00",
        "assets",
        "%61ssets",
    ]
)
_TRAVERSAL_TARGETS = st.sampled_from(
    ["index.js", "index.html", "operator-notes.txt", "etc/passwd", "ws", "health", ""]
)
_TRAVERSAL_ATTEMPTS = st.builds(
    lambda segments, target: "/assets/" + "/".join([*segments, target]),
    st.lists(_TRAVERSAL_SEGMENTS, max_size=4),
    _TRAVERSAL_TARGETS,
)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(attempts=st.lists(_TRAVERSAL_ATTEMPTS, min_size=1, max_size=6))
def test_gateway_serves_no_token_free_body_from_outside_the_bundle_directory(
    tmp_path: Path, attempts: list[str]
) -> None:
    with _asset_gateway(tmp_path) as gateway:
        bodies = {path: _fetch_body(gateway.bound_port, path) for path in attempts}

    for path, (status, body) in bodies.items():
        assert status in {200, 403, 404}, path
        # `assets_dir/assets/` holds exactly one file, so a token-free 200 that
        # returns anything else means the request escaped the bundle directory.
        if status == 200:
            assert body == _BUNDLE_BODY, path


# Every byte a request target can carry. The target is the request line's
# middle field, so only the bytes that delimit that line's fields or end it are
# excluded; everything else is target content the gateway has to answer.
_TARGET_BYTES = st.integers(min_value=0, max_value=255).filter(lambda byte: byte not in b" \t\r\n")
_RAW_TARGETS = st.builds(
    lambda route, token: route + b"?token=" + token,
    st.sampled_from(
        [
            b"/",
            b"/index.html",
            b"/health",
            b"/ws",
            b"/nope",
            b"/assets/index.js",
            b"/assets/../index.html",
        ]
    ),
    st.lists(_TARGET_BYTES, max_size=24).map(bytes),
)


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(targets=st.lists(_RAW_TARGETS, min_size=1, max_size=6))
def test_gateway_answers_every_token_spelling_through_its_own_response_builder(
    tmp_path: Path, targets: list[bytes]
) -> None:
    with _asset_gateway(tmp_path) as gateway:
        answers = {target: _raw_fetch(gateway.bound_port, target) for target in targets}
        expected_policy = _expected_policy(f"ws://127.0.0.1:{gateway.bound_port}")

    for target, (status, headers) in answers.items():
        # A response the gateway did not build carries none of these headers,
        # so the policy is how "this left `_response`" is observable from
        # outside. The generalization of `?token=%C3%A9`: no byte a client can
        # put in the request target may push the handshake onto the library's
        # error path, whatever the comparison or the decoder does with it.
        assert headers.get_all("Content-Security-Policy") == [expected_policy], target
        assert status in {200, 403, 404}, target


def test_gateway_answers_a_request_target_a_url_parser_rejects(tmp_path: Path) -> None:
    with _asset_gateway(tmp_path) as gateway:
        token = gateway.token.encode()
        answers = {
            name: _raw_fetch(gateway.bound_port, target)
            for name, target in (
                # `urlsplit` raises `ValueError` on a malformed IPv6 authority,
                # which left the handshake as an unauthenticated 500 carrying
                # none of the hygiene headers. An origin-form request target is
                # `absolute-path [ "?" query ]` and not a URL, so it is split
                # on the first `?` and never parsed as one.
                ("authority-form, malformed literal", b"//[::/?token=" + token),
                ("absolute-form, malformed literal", b"http://[::1/?token=" + token),
                # Routed by path alone, so an authority in the target names no
                # route rather than being resolved away.
                ("authority-form", b"//host/?token=" + token),
                # `#` is an ordinary query byte in a request target, so it is
                # part of the token rather than a fragment delimiter.
                ("fragment-looking suffix", b"/?token=" + token + b"#fragment"),
            )
        }
        expected_policy = _expected_policy(f"ws://127.0.0.1:{gateway.bound_port}")

    assert {name: status for name, (status, _headers) in answers.items()} == {
        "authority-form, malformed literal": 404,
        "absolute-form, malformed literal": 404,
        "authority-form": 404,
        "fragment-looking suffix": 403,
    }
    for name, (_status, headers) in answers.items():
        assert headers.get_all("Content-Security-Policy") == [expected_policy], name


def _fetch(port: int, path: str) -> tuple[int, HTTPMessage]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status, response.headers
    finally:
        connection.close()


def _raw_fetch(port: int, target: bytes) -> tuple[int, HTTPMessage]:
    """Fetch a request target spelled as raw bytes.

    `HTTPConnection` encodes the target as ASCII, so a byte above 0x7F, or one
    that no URL grammar admits, can only be put on the request line over a bare
    socket. That is the only way to reach the decode `websockets` actually
    performs (`ascii`/`surrogateescape`), which turns such a byte into a lone
    surrogate rather than rejecting it.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(b"GET " + target + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        response = HTTPResponse(client)
        response.begin()
        response.read()
        return response.status, response.headers


def _fetch_body(port: int, path: str) -> tuple[int, bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_gateway_rejects_wrong_origin_and_capability_token(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")

    with WebSocketGateway(parts.api) as gateway:
        asyncio.run(_assert_rejected(gateway.websocket_url, "http://evil.example"))
        asyncio.run(
            _assert_rejected(
                gateway.websocket_url.replace(gateway.token, "wrong-token"),
                f"http://127.0.0.1:{gateway.bound_port}",
            )
        )


def test_gateway_accepts_an_explicit_browser_harness_origin(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")

    with WebSocketGateway(parts.api, allowed_origins=("http://127.0.0.1:5173",)) as gateway:
        response = asyncio.run(_request(gateway, SnapshotQuery(), origin="http://127.0.0.1:5173"))

    assert response["ok"] is True


async def _request(
    gateway: WebSocketGateway,
    request: SnapshotQuery,
    *,
    origin: str | None = None,
) -> dict[str, Any]:
    origin = origin or f"http://127.0.0.1:{gateway.bound_port}"
    async with connect(gateway.websocket_url, origin=cast("Origin", origin)) as websocket:
        await websocket.send(request.model_dump_json())
        return json.loads(await websocket.recv())


async def _assert_rejected(url: str, origin: str) -> None:
    with pytest.raises(InvalidStatus) as failure:
        async with connect(url, origin=cast("Origin", origin)):
            pass
    assert failure.value.response.status_code == 403


def test_gateway_lifecycle_and_asset_edge_cases(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")
    assets = tmp_path / "web"
    assets.mkdir()
    (assets / "assets").mkdir()
    (assets / "assets" / "app.js").write_text("console.log('ok')")

    gateway = WebSocketGateway(parts.api, assets_dir=assets, port=4312)
    with pytest.raises(RuntimeError, match="not running"):
        _ = gateway.url
    with pytest.raises(RuntimeError, match="not running"):
        _ = gateway.websocket_url
    with pytest.raises(RuntimeError, match="not running"):
        _ = gateway.bound_port
    assert gateway._actual_origin() == "http://127.0.0.1:4312"  # noqa: SLF001  # lint-waiver: LW-101023 [SLF001]; exercise the gateway's pre-bind origin calculation

    with gateway:
        with pytest.raises(RuntimeError, match="already running"):
            gateway.start()
        response = asyncio.run(
            gateway._process_request(  # noqa: SLF001  # lint-waiver: LW-101024 [SLF001]; exercise HTTP routing without another network client
                cast("ServerConnection", None),
                _http_request("/assets/app.js?token=ignored"),
            )
        )
        assert response is not None
        assert response.status_code == 200
        missing = asyncio.run(
            gateway._process_request(  # noqa: SLF001  # lint-waiver: LW-101025 [SLF001]; exercise missing-asset handling directly
                cast("ServerConnection", None),
                _http_request("/assets/missing.js"),
            )
        )
        assert missing is not None
        assert missing.status_code == 404
        # Traversal out of `/assets/` is covered over real HTTP by
        # `test_gateway_requires_the_token_for_paths_reachable_only_by_traversal`.
        not_found = asyncio.run(
            gateway._process_request(  # noqa: SLF001  # lint-waiver: LW-101027 [SLF001]; exercise unknown-route handling directly
                cast("ServerConnection", None),
                _http_request(f"/other?token={gateway.token}"),
            )
        )
        assert not_found is not None
        assert not_found.status_code == 404

    with gateway:
        assert gateway.bound_port > 0

    no_assets = WebSocketGateway(parts.api)
    missing_root = asyncio.run(
        no_assets._process_request(  # noqa: SLF001  # lint-waiver: LW-101028 [SLF001]; exercise the missing-assets response directly
            cast("ServerConnection", None),
            _http_request(f"/?token={no_assets.token}"),
        )
    )
    assert missing_root is not None
    assert missing_root.status_code == 404


def test_gateway_handles_text_protocol_errors_and_subscriptions(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")
    gateway = WebSocketGateway(parts.api)

    class FakeConnection:
        def __init__(self, frames: list[object]) -> None:
            self.frames = iter(frames)
            self.sent: list[str] = []
            self.state = "OPEN"

        def __aiter__(self) -> FakeConnection:
            return self

        async def __anext__(self) -> object:
            try:
                return next(self.frames)
            except StopIteration:
                raise StopAsyncIteration from None

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def close(self) -> None:
            self.state = "CLOSED"

    binary = FakeConnection([b"binary"])
    asyncio.run(
        gateway._handle_connection(  # noqa: SLF001  # lint-waiver: LW-101029 [SLF001]; drive the transport boundary with a binary frame
            cast("ServerConnection", binary)
        )
    )
    assert json.loads(binary.sent[0])["code"] == "invalid_frame"

    malformed = FakeConnection([])
    assert (
        asyncio.run(
            gateway._handle_request(  # noqa: SLF001  # lint-waiver: LW-101030 [SLF001]; drive malformed-frame handling without a network client
                cast("ServerConnection", malformed), "{"
            )
        )
        is False
    )
    assert json.loads(malformed.sent[0])["ok"] is False
    assert _request_id("not-json") == "unknown"
    assert _request_id("[]") == "unknown"
    assert _connection_closed(SimpleNamespace(state="CLOSED")) is True
    assert _connection_closed(SimpleNamespace(state="OPEN")) is False
    assert _connection_closed(SimpleNamespace(state=State.CLOSED)) is True
    assert _connection_closed(SimpleNamespace(state=State.OPEN)) is False
    assert _content_type(Path("file.json")) == "application/json; charset=utf-8"
    assert _content_type(Path("file.svg")) == "image/svg+xml"
    assert _content_type(Path("file.bin")) == "application/octet-stream"

    async def subscribe() -> list[dict[str, Any]]:
        origin = f"http://127.0.0.1:{gateway.bound_port}"
        messages: list[dict[str, Any]] = []
        async with connect(gateway.websocket_url, origin=cast("Origin", origin)) as websocket:
            await websocket.send(SubscribeRequest(client_id="browser-client").model_dump_json())
            messages.append(json.loads(await websocket.recv()))
            messages.append(json.loads(await websocket.recv()))
        return messages

    with gateway:
        messages = asyncio.run(subscribe())
    assert messages[0]["type"] == "subscribed"
    assert messages[0]["client_id"] == "browser-client"
    assert messages[1]["type"] == "event_batch"


def test_gateway_reports_subscription_bootstrap_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = build_server_parts(tmp_path / "logs")

    def fail_bootstrap(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("boom")

    # test-isolation: force bootstrap failure to exercise the protocol error response
    monkeypatch.setattr(
        parts.api,
        "subscription_bootstrap",
        fail_bootstrap,
    )

    async def request() -> dict[str, Any]:
        origin = f"http://127.0.0.1:{gateway.bound_port}"
        async with connect(gateway.websocket_url, origin=cast("Origin", origin)) as websocket:
            await websocket.send(
                SubscribeRequest(client_id="failing-browser-client").model_dump_json()
            )
            return json.loads(await websocket.recv())

    with WebSocketGateway(parts.api) as gateway:
        response = asyncio.run(request())
    assert response["type"] == "protocol_error"
    assert response["client_id"] == "failing-browser-client"
    assert response["code"] == "stream_failed"


class _HalfOpenPeer:
    """A subscriber that completes the handshake and then never answers.

    Hand-framed on a raw socket on purpose. Every WebSocket client library,
    including ``websockets``' own, answers a protocol ping automatically below
    its public API, so no library client can produce the peer this contract is
    about: one that is still connected at the TCP level but will never pong
    and, in ``stop_reading`` mode, never drain its socket either.
    """

    def __init__(self, gateway: WebSocketGateway, *, receive_buffer: int | None = None) -> None:
        """Handshake against *gateway*, optionally capping the receive window."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if receive_buffer is not None:
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        self._socket.settimeout(10)
        self._socket.connect(("127.0.0.1", gateway.bound_port))
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self._socket.sendall(
            (
                f"GET /ws?token={gateway.token} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{gateway.bound_port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Origin: http://127.0.0.1:{gateway.bound_port}\r\n"
                "\r\n"
            ).encode()
        )
        self._buffer = b""
        while b"\r\n\r\n" not in self._buffer:
            self._buffer += self._recv()
        head, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        assert status == b"HTTP/1.1 101 Switching Protocols", status

    def subscribe(self) -> None:
        """Send one masked subscribe frame."""
        payload = SubscribeRequest(client_id="half-open-peer").model_dump_json().encode()
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        length = (
            bytes([0x80 | len(payload)])
            if len(payload) < _EXTENDED_LENGTH
            else bytes([0x80 | _EXTENDED_LENGTH]) + struct.pack("!H", len(payload))
        )
        self._socket.sendall(bytes([0x81]) + length + mask + masked)

    def receive_text(self) -> dict[str, Any]:
        """Read the next server text frame, ignoring control frames."""
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x1:
                return cast("dict[str, Any]", json.loads(payload))

    def close(self) -> None:
        """Drop the socket."""
        self._socket.close()

    def _recv(self) -> bytes:
        chunk = self._socket.recv(65536)
        assert chunk, "gateway closed the connection before the expected frame"
        return chunk

    def _take(self, count: int) -> bytes:
        while len(self._buffer) < count:
            self._buffer += self._recv()
        taken, self._buffer = self._buffer[:count], self._buffer[count:]
        return taken

    def _read_frame(self) -> tuple[int, bytes]:
        header = self._take(2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == _EXTENDED_LENGTH:
            length = struct.unpack("!H", self._take(2))[0]
        elif length == _EXTENDED_LENGTH_64:
            length = struct.unpack("!Q", self._take(8))[0]
        return opcode, self._take(length)


def _reaped(wait: Callable[[], None], *, ceiling: float = 10.0) -> bool:
    """Whether *wait* observes the last subscription ending.

    The ceiling only keeps a regression from hanging the suite: the assertion
    is on the boolean, never on how long it took. Nothing competes with the
    waiter, so the outcome does not depend on how fast the host is.
    """
    observed = threading.Event()

    def run() -> None:
        wait()
        observed.set()

    threading.Thread(target=run, name="reap-waiter", daemon=True).start()
    return observed.wait(timeout=ceiling)


def test_a_half_open_subscriber_is_reaped_by_the_keepalive_and_lets_the_run_finish(
    tmp_path: Path, socket_dir: Path
) -> None:
    """The keepalive alone must carry an idle half-open peer through to run exit.

    The write deadline is left far above the ceiling so it cannot be what ends
    this connection: the only mechanism under test is ping, ping timeout, and
    the closing handshake giving up on a peer that never echoes the close.
    """
    limits = WebSocketLimits(
        ping_interval_seconds=0.1,
        ping_timeout_seconds=0.1,
        close_timeout_seconds=0.1,
        write_deadline_seconds=300.0,
    )
    parts = build_server_parts(tmp_path / "logs")
    tracker = SubscriptionTracker()
    try:
        with (
            UnixJsonlServer(socket_dir / "half-open.sock", parts.api, tracker) as unix,
            WebSocketGateway(parts.api, subscriptions=tracker, limits=limits) as gateway,
        ):
            peer = _HalfOpenPeer(gateway)
            try:
                peer.subscribe()
                assert peer.receive_text()["type"] == "subscribed"
                # ``wait_for_subscriber_disconnect`` is the exact call
                # ``ServerRuntime`` makes to decide a non-detached run may
                # finish, over the tracker both transports share.
                assert _reaped(unix.wait_for_subscriber_disconnect)
            finally:
                peer.close()
    finally:
        parts.close()


def test_a_subscriber_that_stops_draining_is_abandoned_at_the_write_deadline(
    tmp_path: Path, socket_dir: Path
) -> None:
    """Regression: a stalled send must not also block the reaping keepalive.

    ``websockets`` ends every write in ``drain()``, which suspends while the
    transport is over its high-water mark. A peer that neither drains its
    socket nor answers pings therefore stalled the gateway's stream loop *and*
    the library's own keepalive ping and close frames, so before the write
    deadline this subscription was never released and a non-detached run never
    exited. Measured against the unfixed gateway the tracker still reported
    the peer as active after 90 seconds.

    The keepalive is pushed out past the ceiling here, so the write deadline is
    the only thing that can end the connection. That also makes the pass an
    assertion about the producer stall itself: had the burst been buffered
    instead of stalling, the send would have completed and no deadline could
    have fired.
    """
    limits = WebSocketLimits(
        ping_interval_seconds=600.0,
        ping_timeout_seconds=600.0,
        close_timeout_seconds=600.0,
        write_deadline_seconds=0.3,
        send_buffer_bytes=1024,
    )
    parts = build_server_parts(tmp_path / "logs")
    tracker = SubscriptionTracker()
    try:
        with (
            UnixJsonlServer(socket_dir / "stalled.sock", parts.api, tracker) as unix,
            WebSocketGateway(parts.api, subscriptions=tracker, limits=limits) as gateway,
        ):
            peer = _HalfOpenPeer(gateway, receive_buffer=_PEER_RECEIVE_BUFFER_BYTES)
            try:
                for index in range(_STALL_EVENTS):
                    parts.journal.publish_output(
                        "stdout", f"{index:04d}" + "x" * _STALL_EVENT_BYTES
                    )
                peer.subscribe()
                # The acknowledgement precedes the replay, so it arrives before
                # the buffers fill; the replay behind it is what stalls.
                assert peer.receive_text()["type"] == "subscribed"
                assert _reaped(unix.wait_for_subscriber_disconnect)
            finally:
                peer.close()
    finally:
        parts.close()


def test_a_write_that_never_drains_abandons_the_peer_and_frees_its_subscription(
    tmp_path: Path,
) -> None:
    """The send-side overflow policy, without depending on any socket's buffers."""
    limits = WebSocketLimits(write_deadline_seconds=0.05)
    parts = build_server_parts(tmp_path / "logs")
    tracker = SubscriptionTracker()
    gateway = WebSocketGateway(parts.api, subscriptions=tracker, limits=limits)

    class FakeTransport:
        def __init__(self) -> None:
            self.aborts = 0

        def abort(self) -> None:
            self.aborts += 1

    class StalledConnection:
        """A connection whose writes never complete, as a paused transport's do."""

        def __init__(self, frames: list[str]) -> None:
            self.frames = iter(frames)
            self.transport = FakeTransport()
            self.state = "OPEN"

        def __aiter__(self) -> StalledConnection:
            return self

        async def __anext__(self) -> str:
            try:
                return next(self.frames)
            except StopIteration:
                raise StopAsyncIteration from None

        async def send(self, message: str) -> None:
            del message
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.state = "CLOSED"

    connection = StalledConnection([SubscribeRequest(client_id="stalled").model_dump_json()])

    async def drive() -> None:
        # The ceiling turns a missing deadline into a failure instead of a
        # hung suite. It cannot mask one: the deadline under test is 0.05s.
        await asyncio.wait_for(
            gateway._handle_connection(  # noqa: SLF001  # lint-waiver: LW-101109 [SLF001]; drive the write deadline without a socket whose buffers the test cannot bound
                cast("ServerConnection", connection)
            ),
            timeout=10.0,
        )

    asyncio.run(drive())

    assert connection.transport.aborts >= 1
    assert _reaped(lambda: tracker.wait_for_none_active(settle_seconds=0.0))
    parts.close()


def test_gateway_publishes_and_cleans_project_instance_record(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")
    assets = tmp_path / "web"
    assets.mkdir()
    (assets / "index.html").write_text("ok")
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"

    with WebSocketGateway(parts.api, assets_dir=assets, instance_path=instance_path) as gateway:
        record = WebInstanceRecord.discover(instance_path)
        assert record is not None
        assert record.url == gateway.url
        health_url = f"http://127.0.0.1:{gateway.bound_port}/health?token={gateway.token}"
        with urlopen(health_url) as response:  # noqa: S310  # lint-waiver: LW-101060 [S310]; connect only to the loopback health URL captured from the gateway under test
            assert response.status == 200
            assert response.read() == b"vibesys-ok\n"

    assert not instance_path.exists()
