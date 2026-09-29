"""A real home server on an ephemeral loopback port, with injected host state."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.entrypoints.web_home.support import Home

from entrypoints.web_home.app import HomeServer
from entrypoints.web_home.context import HomeConfig

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def home(tmp_path: Path) -> Iterator[Home]:
    assets = tmp_path / "dist"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("<!doctype html><title>VibeSys</title>\n")
    (assets / "assets" / "app.js").write_text("console.log('app');\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    config = HomeConfig(
        state_home=tmp_path / "state",
        roots=(workspace.resolve(),),
        dotenv_path=tmp_path / "checkout" / ".env",
        assets_dir=assets.resolve(),
        port=0,
        dev_origins=("http://127.0.0.1:5173",),
        # The spawned run server must resolve runs in the same isolated state home.
        environ={
            "HOME": str(user_home),
            "PATH": "/usr/bin:/bin",
            "VIBESYS_STATE_HOME": os.environ["VIBESYS_STATE_HOME"],
        },
        clock=lambda: datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
    )
    server = HomeServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Home(
            config=config,
            workspace=workspace.resolve(),
            default_headers={
                "Authorization": f"Bearer {config.token}",
                "Origin": config.origin,
                "Content-Type": "application/json",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
