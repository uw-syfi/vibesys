"""Fetch and verify the official Bun runtime for a release target."""

from __future__ import annotations

import argparse
import hashlib
import io
import stat
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Never

from tui_packaging import BUN_VERSION
from wheel_targets import TARGETS

REPO_ROOT = Path(__file__).resolve().parents[1]

BUN_RELEASE_ROOT = "https://github.com/oven-sh/bun/releases/download"
Downloader = Callable[[str], bytes]


class BunFetchError(RuntimeError):
    """Raised when the pinned Bun artifact cannot be verified and extracted."""

    @classmethod
    def unsupported_target(cls, target_key: str) -> BunFetchError:
        """Build an error for an unknown wheel target."""
        return cls(f"Unsupported wheel target: {target_key}")

    @classmethod
    def download_failed(cls, url: str) -> BunFetchError:
        """Build an error for a failed official archive download."""
        return cls(f"Cannot download pinned Bun archive: {url}")

    @classmethod
    def invalid_zip(cls) -> BunFetchError:
        """Build an error for a malformed archive."""
        return cls("Bun archive is not a valid ZIP file")


def fetch_bun(
    target_key: str,
    output: Path,
    *,
    downloader: Downloader | None = None,
    expected_sha256: str | None = None,
) -> Path:
    """Download, checksum, and extract one target's Bun executable."""
    try:
        target = TARGETS[target_key]
    except KeyError as exc:
        raise BunFetchError.unsupported_target(target_key) from exc
    url = f"{BUN_RELEASE_ROOT}/bun-v{BUN_VERSION}/{target.bun_asset}"
    try:
        payload = (downloader or _download)(url)
    except (OSError, urllib.error.URLError) as exc:
        raise BunFetchError.download_failed(url) from exc
    expected = expected_sha256 or target.bun_sha256
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        _fail(f"Bun archive SHA-256 mismatch: expected {expected}, found {actual}")

    member = f"{target.bun_asset.removesuffix('.zip')}/bun"
    directory = f"{target.bun_asset.removesuffix('.zip')}/"
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            files = [info.filename for info in infos if not info.is_dir()]
            directories = [info.filename for info in infos if info.is_dir()]
            if files != [member] or directories not in ([], [directory]):
                _fail(f"Bun archive does not contain only the expected member {member!r}")
            executable = archive.read(member)
    except zipfile.BadZipFile as exc:
        raise BunFetchError.invalid_zip() from exc

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(executable)
    output.chmod(output.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return output


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        return response.read()


def _fail(message: str) -> Never:
    raise BunFetchError(message)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    """Fetch the selected target runtime with concise diagnostics."""
    args = _parse_args()
    try:
        executable = fetch_bun(args.target, args.output)
    except BunFetchError as exc:
        print(f"Bun fetch failed: {exc}", file=sys.stderr)
        return 1
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
