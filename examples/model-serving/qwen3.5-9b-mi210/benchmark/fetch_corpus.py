#!/usr/bin/env python3
"""Download and build the benchmark token corpus (`corpus.txt`).
session_runner draws prompt token ids from this text (`--text-file`). The
corpus is eight Project Gutenberg ebooks concatenated byte for byte in
filename order, followed by a second copy of 1342-0.txt (Pride and
Prejudice). The measured runs used exactly this file: it was built as
`cat *.txt` in a directory that also held `corpus_raw.txt`, a copy of
1342-0.txt that sorts last. Every download and the result are checked
against pinned sha256 digests.
    python3 benchmark/fetch_corpus.py [--out PATH]
Default output: $XDG_CACHE_HOME/vibesys/qwen3.5-9b-mi210/corpus.txt
(~/.cache when XDG_CACHE_HOME is unset), which run.py reads when neither
--text-file nor $QWEN35_BENCH_ASSETS is given.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

# (ebook number, sha256 of https://www.gutenberg.org/files/<n>/<n>-0.txt)
EBOOKS: tuple[tuple[int, str], ...] = (
    (11, "a3a27f8edbf7fcd9b8ba8435494440e24952deaa3e2f2d65192d4cb7ca403754"),
    (1342, "81300b79e8a8d65ac530a97578417d06137e3bbc90622a10a65e5036183d2500"),
    (1661, "b9c105a5c0cf9fcb37c7d0211658851ed56074519c9455d15e20f04c0c5a3174"),
    (174, "7782dd93e6a457979ef451735c9fe4bd666968c314859d545f0d27a8c9b86aa2"),
    (2701, "547de799c1643ef231cc1efe45ffccefcb876ff3fe8f2206e7488529ad08f999"),
    (345, "8cf05c0f0ac5b282d84f2fb36dbccd744bdb5bce7b5612438a63b43cbe27b472"),
    (84, "06c37d2c52d208d3d81eb12c3b10b5edbd7728b73554325ddceadbe2fb427e77"),
    (98, "e3dfeb67feb904ac0f73a35204a175c58248422c2be8556802cb8820d958d67c"),
)
# Concatenation order, by ebook number (1342 appears twice; see module docstring).
ORDER: tuple[int, ...] = (11, 1342, 1661, 174, 2701, 345, 84, 98, 1342)
CORPUS_SHA256 = "9e0f4bd56d08e150104156d9f0100c4db2064af89ce6ad9d14669c4b071d182c"
CORPUS_BYTES = 5979406


class CorpusError(RuntimeError):
    """A download failed or a digest did not match its pin."""


def default_path() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "vibesys" / "qwen3.5-9b-mi210" / "corpus.txt"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_valid(path: Path) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == CORPUS_BYTES
        and sha256(path.read_bytes()) == CORPUS_SHA256
    )


def download(number: int, expected: str, timeout_s: float) -> bytes:
    url = f"https://www.gutenberg.org/files/{number}/{number}-0.txt"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            data = response.read()
    except OSError as exc:
        raise CorpusError(f"GET {url} failed: {exc}") from exc
    if sha256(data) != expected:
        raise CorpusError(f"{url}: sha256 {sha256(data)} != pinned {expected}")
    return data


def build(out: Path, timeout_s: float = 60.0) -> Path:
    """Write the verified corpus to `out` unless a valid copy is already there."""
    if is_valid(out):
        return out
    pins = dict(EBOOKS)
    books = {number: download(number, pins[number], timeout_s) for number in pins}
    corpus = b"".join(books[number] for number in ORDER)
    if sha256(corpus) != CORPUS_SHA256:
        raise CorpusError(f"built corpus sha256 {sha256(corpus)} != pinned {CORPUS_SHA256}")
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_suffix(".partial")
    partial.write_bytes(corpus)
    partial.replace(out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=default_path())
    args = parser.parse_args()
    try:
        path = build(args.out)
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
