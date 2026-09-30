"""Loopback WebSocket gateway contract tests."""

from __future__ import annotations

import asyncio
import json
from http.client import HTTPConnection, HTTPMessage
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from urllib.request import urlopen

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.server.support import build_server_parts
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.protocol import State

from server.api.protocol import SnapshotQuery, SubscribeRequest
from server.transport.discovery import WebInstanceRecord
from server.transport.websocket import (
    WebSocketGateway,
    _connection_closed,
    _content_type,
    _request_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request
    from websockets.typing import Origin


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
                ("unknown-route", f"/nope?token={gateway.token}"),
            )
        }
        expected_policy = _expected_policy(f"ws://127.0.0.1:{gateway.bound_port}")

    assert {name: status for name, (status, _headers) in routes.items()} == {
        "index": 200,
        "asset": 200,
        "health": 200,
        "missing-token": 403,
        "unknown-route": 404,
    }
    for name, (_status, headers) in routes.items():
        assert headers.get_all("Cache-Control") == ["no-store"], name
        assert headers.get_all("Referrer-Policy") == ["no-referrer"], name
        assert headers.get_all("X-Content-Type-Options") == ["nosniff"], name
        assert headers.get_all("Content-Security-Policy") == [expected_policy], name


def test_gateway_derives_the_connect_source_from_its_allowed_origins(tmp_path: Path) -> None:
    origins = ("http://127.0.0.1:5173", "https://localhost:5173")
    with _asset_gateway(tmp_path, allowed_origins=origins) as gateway:
        _status, headers = _fetch(gateway.bound_port, f"/?token={gateway.token}")
        expected_sockets = sorted(
            {
                f"ws://127.0.0.1:{gateway.bound_port}",
                "ws://127.0.0.1:5173",
                "wss://localhost:5173",
            }
        )

    # Every declared browser origin contributes its own WebSocket authority,
    # `https:` as `wss:`, and nothing else. No wildcard port, no bare loopback.
    assert headers.get_all("Content-Security-Policy") == [_expected_policy(*expected_sockets)]


def test_gateway_policy_uses_the_requested_port_before_binding(tmp_path: Path) -> None:
    gateway = _asset_gateway(tmp_path, allowed_origins=("https://localhost:5173",))
    gateway.port = 4312

    # Same pre-bind fallback `_actual_origin` uses, so the policy is well formed
    # on a response served before the listening socket exists.
    policy = gateway._content_security_policy()  # noqa: SLF001  # lint-waiver: LW-101107 [SLF001]; exercise the pre-bind policy without a listening socket

    assert policy == _expected_policy("ws://127.0.0.1:4312", "wss://localhost:5173")


def test_gateway_rejects_a_browser_origin_the_policy_cannot_express(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")

    with pytest.raises(ValueError, match="Not a browser origin"):
        WebSocketGateway(parts.api, allowed_origins=("file:///tmp/page.html",))


def _asset_gateway(tmp_path: Path, *, allowed_origins: Sequence[str] = ()) -> WebSocketGateway:
    parts = build_server_parts(tmp_path / "logs")
    assets = tmp_path / "web"
    (assets / "assets").mkdir(parents=True, exist_ok=True)
    (assets / "index.html").write_text("<!doctype html><title>VibeSys</title>")
    (assets / "assets" / "index.js").write_text("export {};\n")
    # Lives at the assets root, so only a traversal out of `/assets/` reaches it.
    (assets / "operator-notes.txt").write_text("not part of the bundle\n")
    return WebSocketGateway(parts.api, assets_dir=assets, allowed_origins=allowed_origins)


_TRAVERSAL_PATHS = (
    "/assets/index.js",
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
        "/assets/../index.html": 200,
        "/assets/%2e%2e/index.html": 200,
        "/assets/../operator-notes.txt": 404,
        "/assets/%2e%2e/operator-notes.txt": 404,
        "/assets/%2e%2e/%2e%2e/etc/passwd": 404,
    }


_BUNDLE_BODY = b"export {};\n"
_TRAVERSAL_SEGMENTS = st.sampled_from(["..", "%2e%2e", "%2E%2E", ".", "%2e", "assets", "%61ssets"])
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


def _fetch(port: int, path: str) -> tuple[int, HTTPMessage]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status, response.headers
    finally:
        connection.close()


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
