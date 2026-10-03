"""A minimal HTTP service so the run's profile capture has a lifecycle to drive."""

import http.server
import sys

port = int(sys.argv[1])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args: object) -> None:
        return


http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
