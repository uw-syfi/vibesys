"""Tiny Show-o2-compatible mock server for evaluator smoke tests."""

from __future__ import annotations

import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6X6x4sAAAAASUVORK5CYII="
)


class Handler(BaseHTTPRequestHandler):
    server_version = "show-o2-mock/1.0"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({"status": "ok"})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/v1/images/generations":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        timings = {"total_ms": 1.0, "mock_ms": 1.0}
        response_format = payload.get("response_format", "b64_json")
        if response_format == "png":
            self._send_bytes(PNG_BYTES, "image/png", timings)
            return
        if response_format == "ppm":
            self._send_bytes(b"P6\n1 1\n255\n\xff\x00\x00", "image/x-portable-pixmap", timings)
            return
        body = {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}
        if payload.get("include_timings"):
            body["timings_ms"] = timings
        self._send_json(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send_json(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, payload: bytes, content_type: str, timings: dict[str, float]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-ShowO2-Timings-Ms", json.dumps(timings))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Show-o2 mock HTTP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"show-o2 mock listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
