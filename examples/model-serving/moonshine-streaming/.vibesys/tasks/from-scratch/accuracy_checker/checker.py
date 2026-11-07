"""Accuracy checker for the moonshine-streaming-medium server (service-style).

Sends every test clip to the running server's offline transcription endpoint,
`POST /v1/audio/transcriptions`, with the request the benchmark's offline mode
sends, and compares each transcript with the Hugging Face reference transcript of
that clip stored in `references.json` beside this file (see the README for how it
was made). A clip passes when the two are equal after normalization (lowercase,
punctuation stripped, whitespace collapsed) or share at least `--threshold` of
their words (word-set overlap, default 0.7). Exit 0 iff every clip passes.

The checker imports nothing from the candidate, loads no model, and needs only
Python's standard library, its own directory and the clip directory, so it runs
next to the server wherever the run environment puts the server.

Usage (the server must already be running):

    python checker.py [--url http://localhost:8000] [--audio-dir ../test_audio]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
# The form fields the benchmark's offline mode sends with each clip.
FORM_FIELDS = {"model": "moonshine"}

#: Sends one request body to a URL and returns the response body; raises OSError on a
#: transport failure or a non-2xx status.
Post = Callable[[str, bytes, str], bytes]


class Clip(NamedTuple):
    file: str
    wav: bytes
    reference: str


def load_clips(references: Path, audio_dir: Path) -> list[Clip]:
    """The clips `references.json` names, read from `audio_dir` and checked by digest."""
    clips = []
    for entry in json.loads(references.read_text(encoding="utf-8"))["clips"]:
        path = audio_dir / entry["file"]
        wav = path.read_bytes()
        digest = hashlib.sha256(wav).hexdigest()
        if digest != entry["sha256"]:
            message = (
                f"{path}: sha256 {digest} differs from {entry['sha256']} in {references}; "
                "regenerate the references with make_references.py"
            )
            raise ValueError(message)
        clips.append(Clip(entry["file"], wav, entry["text"]))
    return clips


# ---------- transport ----------


def encode_form(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body with `fields` and `content` as the `file` part."""
    boundary = f"vibesys-{hashlib.sha256(content).hexdigest()}"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def http_post(url: str, body: bytes, content_type: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": content_type}
    )
    # The server is colocated; never route the request through an environment proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return response.read()


def transcribe(post: Post, url: str, clip: Clip) -> str:
    body, content_type = encode_form(FORM_FIELDS, clip.file, clip.wav)
    payload = json.loads(post(url, body, content_type))
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        message = f"response has no string 'text' field: {str(payload)[:200]!r}"
        raise ValueError(message)
    return text.strip()


# ---------- text comparison ----------


def normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def word_overlap_ratio(ref: str, hyp: str) -> float:
    rw = set(normalize_text(ref).split())
    hw = set(normalize_text(hyp).split())
    if not rw and not hw:
        return 1.0
    if not rw or not hw:
        return 0.0
    return len(rw & hw) / len(rw | hw)


def compare_outputs(ref_text: str, custom_text: str, threshold: float):
    if normalize_text(ref_text) == normalize_text(custom_text):
        return True, f"EXACT match: {ref_text[:80]!r}"
    overlap = word_overlap_ratio(ref_text, custom_text)
    detail = f"  Reference: {ref_text[:100]!r}\n  Custom:    {custom_text[:100]!r}"
    if overlap >= threshold:
        return True, f"PASS (word overlap {overlap:.1%} >= {threshold:.0%}):\n{detail}"
    return False, f"MISMATCH (word overlap {overlap:.1%} < {threshold:.0%}):\n{detail}"


# ---------- main ----------


def run_gate(clips: list[Clip], transcribe_clip: Callable[[Clip], str], threshold: float) -> bool:
    total = len(clips)
    passed = 0
    print("=" * 70)
    print(f"Running {total} test cases (threshold={threshold:.0%} word overlap)")
    print("=" * 70)
    for i, clip in enumerate(clips, 1):
        print(f"\n[{i}/{total}] {clip.file}")
        try:
            custom_text = transcribe_clip(clip)
        except (OSError, ValueError) as exc:
            print(f"  FAIL - request failed: {exc}")
            continue
        ok, detail = compare_outputs(clip.reference, custom_text, threshold)
        print(("  PASS - " if ok else "  FAIL - ") + detail)
        passed += int(ok)
    print("\n" + "=" * 70)
    print(f"Results: {passed}/{total} passed, {total - passed}/{total} failed")
    print("=" * 70)
    return passed == total


def main(argv: list[str] | None = None, post: Post | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--endpoint", default="/v1/audio/transcriptions")
    parser.add_argument(
        "--audio-dir",
        type=Path,
        default=HERE.parent / "test_audio",
        help="Directory holding the clips references.json names (default: ../test_audio)",
    )
    parser.add_argument("--references", type=Path, default=HERE / "references.json")
    parser.add_argument(
        "--threshold", type=float, default=0.7, help="Minimum word overlap ratio to pass"
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="Seconds per request")
    args = parser.parse_args(argv)

    try:
        clips = load_clips(args.references, args.audio_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Cannot load the test clips: {exc!r}")
        return 1
    url = args.url.rstrip("/") + args.endpoint
    print(f"Transcribing {len(clips)} clips from {args.audio_dir} via {url}\n")
    post = post or partial(http_post, timeout=args.timeout)
    ok = run_gate(clips, partial(transcribe, post, url), args.threshold)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
