"""Exercise browser transport against the real Unix JSONL server."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from tests.server.support import build_server_parts

from entrypoints.browser_gateway import _build_parser
from server.api.protocol import ChatQuery, SnapshotQuery, SubscribeRequest
from server.browser_gateway import build_gateway_app, default_allowed_origins
from server.events import EventType
from server.transport.unix_jsonl import UnixJsonlServer

_ALLOWED_ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def backend(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vs-gw-{uuid.uuid4().hex}.sock"  # noqa: S108
    with UnixJsonlServer(socket_path, parts.api) as transport:
        yield parts, socket_path, transport


def test_default_allowed_origins_includes_host_and_dev_ports() -> None:
    origins = default_allowed_origins("127.0.0.1", 8765, dev_ports=[5173])
    assert {"http://127.0.0.1:8765", "http://localhost:8765", "http://localhost:5173"} <= origins
    assert "http://[::1]:8765" in default_allowed_origins("::1", 8765)


@pytest.mark.asyncio
async def test_gateway_forwards_request_and_returns_backend_response(backend) -> None:  # noqa: ANN001
    _, socket_path, _ = backend
    async with TestClient(TestServer(build_gateway_app(socket_path))) as client:
        response = await client.post("/api/request", data=SnapshotQuery().model_dump_json())
        payload = await response.json()
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["snapshot"]["status"] == "running"


@pytest.mark.asyncio
async def test_gateway_sanitizes_backend_errors_over_http(backend) -> None:  # noqa: ANN001
    parts, socket_path, _ = backend

    def fail_chat(question: str) -> str:
        raise RuntimeError(f"token=super-secret while answering: {question}")  # noqa: TRY003

    parts.chat.install_default_handler(fail_chat)
    async with TestClient(TestServer(build_gateway_app(socket_path))) as client:
        response = await client.post(
            "/api/request", data=ChatQuery(text="what happened?").model_dump_json()
        )
        payload = await response.json()
    assert response.status == 200
    assert payload["ok"] is False
    assert "super-secret" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_gateway_rejects_subscribe_over_http(backend) -> None:  # noqa: ANN001
    _, socket_path, _ = backend
    async with TestClient(TestServer(build_gateway_app(socket_path))) as client:
        response = await client.post("/api/request", data=SubscribeRequest().model_dump_json())
    assert response.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/api/request", "/api/events"])
async def test_gateway_rejects_disallowed_origin(backend, endpoint) -> None:  # noqa: ANN001
    _, socket_path, _ = backend
    app = build_gateway_app(socket_path, allowed_origins=[_ALLOWED_ORIGIN])
    async with TestClient(TestServer(app)) as client:
        response = await client.request(
            "POST" if endpoint == "/api/request" else "GET",
            endpoint,
            data=SnapshotQuery().model_dump_json(),
            headers={"Origin": "http://evil.example"},
        )
    assert response.status == 403


@pytest.mark.asyncio
async def test_gateway_websocket_relays_replay_and_live_events(backend) -> None:  # noqa: ANN001
    parts, socket_path, _ = backend
    app = build_gateway_app(socket_path, allowed_origins=[_ALLOWED_ORIGIN])
    async with (
        TestClient(TestServer(app)) as client,
        client.ws_connect("/api/events", headers={"Origin": _ALLOWED_ORIGIN}) as ws,
    ):
        await ws.send_str(SubscribeRequest(after_sequence=0).model_dump_json())
        subscribed = await ws.receive_json(timeout=2)
        replay = await ws.receive_json(timeout=2)
        assert subscribed["type"] == "subscribed"
        assert replay["type"] == "event_batch"
        assert any(event["type"] == "server_started" for event in replay["events"])
        with parts.condition:
            parts.journal.record(EventType.CHAT, "hello", status="answered")
        streamed = await ws.receive_json(timeout=2)
        assert [event["type"] for event in streamed["events"]] == ["chat"]


@pytest.mark.asyncio
async def test_gateway_keepalive_holds_backend_attached(backend) -> None:  # noqa: ANN001
    _, socket_path, transport = backend
    async with TestClient(TestServer(build_gateway_app(socket_path))):
        assert await asyncio.to_thread(transport.wait_for_subscriber, 2)


@pytest.mark.asyncio
async def test_large_response_and_event_exceed_default_stream_limit(backend) -> None:  # noqa: ANN001
    parts, socket_path, _ = backend
    text = "x" * 100_000
    parts.chat.install_default_handler(lambda _: text)
    async with TestClient(TestServer(build_gateway_app(socket_path))) as client:
        response = await client.post("/api/request", data=ChatQuery(text="large").model_dump_json())
        assert response.status == 200
        assert text in await response.text()
        async with client.ws_connect("/api/events") as ws:
            await ws.send_str(SubscribeRequest().model_dump_json())
            await ws.receive_json(timeout=2)
            replay = await ws.receive_json(timeout=2)
            assert text in json.dumps(replay)


def test_entrypoint_rejects_non_loopback_bind() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--control-socket", "test.sock", "--host", "0.0.0.0"])  # noqa: S104


@pytest.mark.asyncio
async def test_shutdown_closes_active_websockets(backend) -> None:  # noqa: ANN001
    _, socket_path, _ = backend
    server = TestServer(build_gateway_app(socket_path))
    await server.start_server()
    try:
        async with (
            ClientSession() as client,
            client.ws_connect(server.make_url("/api/events")) as ws,
        ):
            await ws.send_str(SubscribeRequest().model_dump_json())
            await ws.receive_json(timeout=2)
            await ws.receive_json(timeout=2)
            closing = asyncio.create_task(server.close())
            assert (await ws.receive(timeout=2)).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
            await asyncio.wait_for(closing, timeout=2)
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"\xff", b"{", b'{"type":"query.unknown"}'])
async def test_invalid_requests_return_protocol_errors(backend, body) -> None:  # noqa: ANN001
    _, socket_path, _ = backend
    async with TestClient(TestServer(build_gateway_app(socket_path))) as client:
        response = await client.post("/api/request", data=body)
        assert response.status == 400
        assert (await response.json())["ok"] is False


@pytest.mark.asyncio
async def test_invalid_subscription_returns_protocol_error(backend) -> None:  # noqa: ANN001
    _, socket_path, _ = backend
    async with (
        TestClient(TestServer(build_gateway_app(socket_path))) as client,
        client.ws_connect("/api/events") as ws,
    ):
        await ws.send_json({"type": "query.snapshot"})
        assert (await ws.receive_json(timeout=2))["type"] == "protocol_error"
