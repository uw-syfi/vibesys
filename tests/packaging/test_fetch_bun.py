"""Contracts for fetching the pinned Bun runtime used in release wheels."""

from __future__ import annotations

import hashlib
import http.server
import io
import threading
import zipfile
from typing import TYPE_CHECKING

import pytest
from bun_targets import TARGETS
from fetch_bun import BunFetchError, _download, fetch_bun, host_target

from vs_sim.api.testing import join_or_fail

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
    payload = _bun_archive(target.asset)
    seen_urls: list[str] = []

    def download(url: str) -> bytes:
        seen_urls.append(url)
        return payload

    output = tmp_path / "bin" / "bun"
    result = fetch_bun(
        "linux-x86_64",
        output,
        downloader=download,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert seen_urls == [
        "https://github.com/oven-sh/bun/releases/download/bun-v1.4.2/bun-linux-x64-baseline.zip"
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


@pytest.mark.parametrize(
    ("system", "machine", "target"),
    [
        ("Darwin", "arm64", "macos-arm64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "AMD64", "linux-x86_64"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Linux", "arm64", "linux-aarch64"),
    ],
)
def test_host_target_names_a_pinned_archive_for_every_supported_host(
    system: str, machine: str, target: str
) -> None:
    assert host_target(system, machine) == target
    assert target in TARGETS


@pytest.mark.parametrize(("system", "machine"), [("Darwin", "x86_64"), ("Windows", "AMD64")])
def test_host_target_refuses_a_host_with_no_pinned_archive(system: str, machine: str) -> None:
    with pytest.raises(BunFetchError, match="Unsupported Bun target"):
        host_target(system, machine)


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
        join_or_fail(thread)
