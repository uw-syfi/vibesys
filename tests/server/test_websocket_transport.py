"""Loopback WebSocket gateway contract tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from urllib.request import urlopen

import pytest
from tests.server.support import build_server_parts
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from server.api.protocol import SnapshotQuery, SubscribeRequest
from server.transport.discovery import WebInstanceRecord
from server.transport.websocket import (
    WebSocketGateway,
    _connection_closed,
    _content_type,
    _request_id,
)

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request
    from websockets.typing import Origin


def _http_request(path: str) -> Request:
    return cast("Request", SimpleNamespace(path=path, headers={}))


def test_gateway_serves_assets_and_round_trips_protocol_frames(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")
    assets = tmp_path / "web"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>VibeSys</title>")
    (assets / "assets").mkdir()
    (assets / "assets" / "plex.woff2").write_bytes(b"wOF2\x00\xff")

    with WebSocketGateway(parts.api, assets_dir=assets) as gateway:
        with urlopen(gateway.url, timeout=2) as response:  # noqa: S310  # lint-waiver: LW-101021 [S310]; connect only to the loopback URL produced by the gateway under test
            assert response.status == 200
            assert response.read() == b"<!doctype html><title>VibeSys</title>"
        font_url = gateway.url.replace("/?", "/assets/plex.woff2?")
        with urlopen(font_url, timeout=2) as response:  # noqa: S310  # lint-waiver: LW-101222 [S310]; connect only to the loopback URL produced by the gateway under test
            assert response.headers["Content-Type"] == "font/woff2"
            assert response.read() == b"wOF2\x00\xff"

        response = asyncio.run(_request(gateway, SnapshotQuery()))

    assert response["ok"] is True
    assert response["snapshot"]["status"] == "running"


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
        forbidden = asyncio.run(
            gateway._process_request(  # noqa: SLF001  # lint-waiver: LW-101026 [SLF001]; exercise traversal rejection directly
                cast("ServerConnection", None),
                _http_request("/assets/../secret"),
            )
        )
        assert forbidden is not None
        assert forbidden.status_code == 404
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
    assert _content_type(Path("file.json")) == "application/json; charset=utf-8"
    assert _content_type(Path("file.svg")) == "image/svg+xml"
    assert _content_type(Path("file.bin")) == "application/octet-stream"

    async def subscribe() -> list[dict[str, Any]]:
        origin = f"http://127.0.0.1:{gateway.bound_port}"
        messages: list[dict[str, Any]] = []
        async with connect(gateway.websocket_url, origin=cast("Origin", origin)) as websocket:
            await websocket.send(SubscribeRequest().model_dump_json())
            messages.append(json.loads(await websocket.recv()))
            messages.append(json.loads(await websocket.recv()))
        return messages

    with gateway:
        messages = asyncio.run(subscribe())
    assert messages[0]["type"] == "subscribed"
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
            await websocket.send(SubscribeRequest().model_dump_json())
            return json.loads(await websocket.recv())

    with WebSocketGateway(parts.api) as gateway:
        response = asyncio.run(request())
    assert response["type"] == "protocol_error"
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
