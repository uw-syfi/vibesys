"""Compose the local browser gateway against an existing backend socket."""

from __future__ import annotations

import argparse
from pathlib import Path

from aiohttp import web

from server.browser_gateway import build_gateway_app, default_allowed_origins


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-socket", type=Path, required=True)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dev-origin", action="append", default=[], metavar="ORIGIN")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Serve until interrupted; backend process ownership stays with its launcher."""
    args = _build_parser().parse_args(argv)
    origins = set(default_allowed_origins(args.host, args.port, dev_ports=(5173,)))
    origins.update(args.dev_origin)
    app = build_gateway_app(args.control_socket, allowed_origins=origins)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
