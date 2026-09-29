from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from entrypoints.web import _parser
from entrypoints.web_home.app import DEFAULT_PORT, save_port, saved_port
from server.runtime import WebInstanceRecord

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from tests.entrypoints.web_home.support import Home


def test_index_needs_the_capability_token_and_carries_security_headers(home: Home) -> None:
    assert home.send("GET", "/", headers={}).json()["error"]["code"] == "unauthorized"

    reply = home.send("GET", f"/?token={home.config.token}", headers={})

    assert reply.status == 200
    assert b"<title>VibeSys</title>" in reply.body
    assert "default-src 'self'" in reply.headers["Content-Security-Policy"]
    assert "connect-src 'self' ws://127.0.0.1:*" in reply.headers["Content-Security-Policy"]
    assert reply.headers["Referrer-Policy"] == "no-referrer"
    assert reply.headers["Cache-Control"] == "no-store"


def test_built_assets_are_served_without_a_token_but_only_below_assets(home: Home) -> None:
    assert home.send("GET", "/assets/app.js", headers={}).status == 200
    assert home.send("GET", "/assets/%2e%2e/index.html", headers={}).status == 404
    assert home.send("GET", "/assets/missing.js", headers={}).status == 404


def test_api_requires_the_bearer_token(home: Home) -> None:
    assert home.send("GET", "/api/nope", headers={}).status == 401
    wrong = {"Authorization": "Bearer wrong"}
    assert home.send("GET", "/api/nope", headers=wrong).status == 401
    assert home.get("/api/nope").json()["error"]["code"] == "not_found"


def test_state_changing_requests_need_an_exact_allowed_origin(home: Home) -> None:
    token = {"Authorization": f"Bearer {home.config.token}"}
    for origin in (None, "null", "http://evil.test", "http://localhost:5173"):
        headers = token if origin is None else {**token, "Origin": origin}
        reply = home.send("POST", "/api/nope", {}, headers=headers)
        assert reply.json()["error"]["code"] == "forbidden_origin", origin
    for origin in (home.config.origin, "http://127.0.0.1:5173"):
        reply = home.send("POST", "/api/nope", {}, headers={**token, "Origin": origin})
        assert reply.json()["error"]["code"] == "not_found", origin


def test_a_foreign_host_header_is_rejected_even_for_tokenless_assets(home: Home) -> None:
    for host in (f"evil.test:{home.config.port}", f"localhost:{home.config.port}"):
        reply = home.send("GET", "/assets/app.js", headers={"Host": host})
        assert reply.json()["error"]["code"] == "forbidden_origin", host


def test_request_logs_never_contain_the_capability_token(
    home: Home, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="entrypoints.web_home.app"):
        home.send("GET", f"/?token={home.config.token}", headers={})
        home.send("GET", "/?token=guess", headers={})
        encoded = home.send("GET", f"/?%74oken={home.config.token}", headers={})

    assert encoded.status == 200
    assert "GET / 200" in caplog.text
    assert home.config.token not in caplog.text
    assert "guess" not in caplog.text
    assert "oken" not in caplog.text


def test_health_answers_the_discovery_probe(home: Home, tmp_path: Path) -> None:
    record = WebInstanceRecord.from_gateway(
        pid=os.getpid(), port=home.config.port, token=home.config.token, project_root=tmp_path
    )
    path = tmp_path / "home.json"
    record.write(path)

    assert WebInstanceRecord.discover(path) == record


def test_the_saved_port_defaults_and_round_trips(tmp_path: Path) -> None:
    assert saved_port(tmp_path) == DEFAULT_PORT
    save_port(tmp_path, 9100)
    assert saved_port(tmp_path) == 9100
    (tmp_path / "home-settings.json").write_text('{"port": "nope"}')
    assert saved_port(tmp_path) == DEFAULT_PORT


def test_web_home_parses_its_options() -> None:
    args = _parser().parse_args(
        ["home", "--port", "9100", "--root", "/srv", "--dev-origin", "http://127.0.0.1:5173"]
    )

    assert (args.command, args.port, [str(r) for r in args.root]) == ("home", 9100, ["/srv"])
    assert args.dev_origin == ["http://127.0.0.1:5173"]
