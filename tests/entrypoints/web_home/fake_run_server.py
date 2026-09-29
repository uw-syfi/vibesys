"""A stand-in for `python -m entrypoints.server --web --detach` used by the runs tests.

It publishes a discovery record shaped like sub-project 1's (`mode`, and
`run_id` only for `--web-reopen-run`), answers `/health?token=`, and checks
`/ws` like the real gateway (`websocket.py`): the capability token, then an
exact `Origin` from `--web-origin` or its own origin. It records its argv next
to the record and removes the record on SIGTERM.

`FAKE_RUN_SERVER_FAIL=1` writes to stderr and exits 2; `FAKE_RUN_SERVER_HANG=1`
never publishes a record.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from types import FrameType


def _option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


class _Gateway(BaseHTTPRequestHandler):
    token: ClassVar[str] = ""
    origins: ClassVar[frozenset[str]] = frozenset()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        token = parse_qs(parsed.query).get("token", [""])[0]
        if not secrets.compare_digest(token, self.token):
            self._reply(403, b"Invalid VibeSys capability token\n")
        elif parsed.path == "/health":
            self._reply(200, b"vibesys-ok\n")
        elif parsed.path == "/ws" and self.headers.get("Origin") not in self.origins:
            self._reply(403, b"Invalid WebSocket origin\n")
        elif parsed.path == "/ws":
            self._reply(200, b"origin-ok\n")
        else:
            self._reply(404, b"Not found\n")

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        del format, args


def main() -> None:
    argv = sys.argv[1:]
    if os.environ.get("FAKE_RUN_SERVER_FAIL") == "1":
        sys.stderr.write("Traceback: boom\nConfigurationError: bad run\n")
        raise SystemExit(2)
    if os.environ.get("FAKE_RUN_SERVER_HANG") == "1":
        signal.pause()
    record_path = Path(_option(argv, "--web-instance") or "")
    reopen = _option(argv, "--web-reopen-run")
    server = HTTPServer(("127.0.0.1", 0), _Gateway)
    port = server.server_address[1]
    _Gateway.token = secrets.token_urlsafe(16)
    allowed = [argv[i + 1] for i, item in enumerate(argv) if item == "--web-origin"]
    _Gateway.origins = frozenset({f"http://127.0.0.1:{port}", *allowed})
    record_path.with_suffix(".argv.json").write_text(json.dumps(argv))
    record = {
        "version": 1,
        "pid": os.getpid(),
        "port": port,
        "token": _Gateway.token,
        "url": f"http://127.0.0.1:{port}/?token={_Gateway.token}",
        "project_root": str(Path.cwd()),
        "started_at": time.time(),
        "run_id": reopen,
        "mode": "reopen" if reopen else "live",
    }
    temporary = record_path.with_name(record_path.name + ".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(record_path)

    def stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        record_path.unlink(missing_ok=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
