"""Contracts for fetching the pinned Bun runtime used in release wheels."""

from __future__ import annotations

import hashlib
import http.server
import io
import threading
import zipfile
from typing import TYPE_CHECKING

import pytest
from fetch_bun import BunFetchError, _download, fetch_bun
from wheel_targets import TARGETS

if TYPE_CHECKING:
    from pathlib import Path


def _bun_archive(asset: str, content: bytes = b"bun executable") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{asset.removesuffix('.zip')}/", b"")
        archive.writestr(f"{asset.removesuffix('.zip')}/bun", content)
    return buffer.getvalue()


def test_fetch_bun_uses_versioned_official_url_and_verifies_before_extraction(
    tmp_path: Path,
) -> None:
    target = TARGETS["linux-x86_64"]
    payload = _bun_archive(target.bun_asset)
    seen_urls: list[str] = []

    def download(url: str) -> bytes:
        seen_urls.append(url)
        return payload

    output = tmp_path / "bin" / "bun"
    result = fetch_bun(
        target.key,
        output,
        downloader=download,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert seen_urls == [
        "https://github.com/oven-sh/bun/releases/download/bun-v1.3.9/bun-linux-x64-baseline.zip"
    ]
    assert result == output.resolve()
    assert result.read_bytes() == b"bun executable"
    assert result.stat().st_mode & 0o111


def test_fetch_bun_rejects_a_checksum_mismatch_before_writing_output(tmp_path: Path) -> None:
    output = tmp_path / "bun"

    with pytest.raises(BunFetchError, match="SHA-256"):
        fetch_bun(
            "linux-x86_64",
            output,
            downloader=lambda _url: _bun_archive("bun-linux-x64-baseline.zip"),
        )

    assert not output.exists()


def test_fetch_bun_rejects_an_archive_without_the_expected_runtime(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("unexpected/bun", b"wrong")
    payload = buffer.getvalue()

    with pytest.raises(BunFetchError, match="expected member"):
        fetch_bun(
            "macos-arm64",
            tmp_path / "bun",
            downloader=lambda _url: payload,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_download_follows_redirects_like_github_release_assets() -> None:
    body = b"archive bytes"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/release":
                self.send_response(302)
                self.send_header("Location", "/cdn")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _download(f"http://127.0.0.1:{server.server_port}/release") == body
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
