"""Loopback WebSocket gateway contract tests."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast
from urllib.request import urlopen

import pytest
from tests.server.support import build_server_parts
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from server.api.protocol import SnapshotQuery
from server.transport.websocket import WebSocketGateway

if TYPE_CHECKING:
    from pathlib import Path

    from websockets.typing import Origin


def test_gateway_serves_assets_and_round_trips_protocol_frames(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")
    assets = tmp_path / "web"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>VibeSys</title>")

    with WebSocketGateway(parts.api, assets_dir=assets) as gateway:
        with urlopen(gateway.url, timeout=2) as response:  # noqa: S310
            assert response.status == 200
            assert response.read() == b"<!doctype html><title>VibeSys</title>"

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


async def _request(gateway: WebSocketGateway, request: SnapshotQuery) -> dict[str, Any]:
    origin = f"http://127.0.0.1:{gateway.bound_port}"
    async with connect(gateway.websocket_url, origin=cast("Origin", origin)) as websocket:
        await websocket.send(request.model_dump_json())
        return json.loads(await websocket.recv())


async def _assert_rejected(url: str, origin: str) -> None:
    with pytest.raises(InvalidStatus) as failure:
        async with connect(url, origin=cast("Origin", origin)):
            pass
    assert failure.value.response.status_code == 403
