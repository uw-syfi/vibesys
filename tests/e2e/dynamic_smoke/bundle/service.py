"""A minimal HTTP service so the run's profile capture has a lifecycle to drive."""

import http.server
import sys

port = int(sys.argv[1])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # lint-waiver: LW-731203 [A002]; the override must keep the stdlib parameter name `format`.
        del format, args


http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
