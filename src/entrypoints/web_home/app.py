"""The `vibesys web home` server: capability checks, routing, assets, and startup."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from urllib.parse import parse_qs, unquote, urlsplit

from entrypoints.web_home import catalog, keys, projects, runs, tasks
from entrypoints.web_home.context import HomeConfig, Request, atomic_write
from entrypoints.web_home.contract import ApiError, ErrorBody, ErrorCode
from server.runtime import WebInstanceClaim, WebInstanceRecord
from vibesys.api import DOTENV_PATH
from vs_project.api import state_home

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from pydantic import BaseModel

_LOG = logging.getLogger(__name__)
DEFAULT_PORT = 8764
_MAX_BODY_BYTES = 1 << 20
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self' ws://127.0.0.1:*; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
_SECURITY_HEADERS = (
    ("Content-Security-Policy", _CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
_STATE_CHANGING = frozenset({"POST", "PUT", "DELETE"})
_PROJECT = r"/api/projects/([^/]+)"
_ROUTES: tuple[tuple[str, re.Pattern[str], Callable[[Request], BaseModel]], ...] = (
    ("GET", re.compile(r"/api/fs"), projects.list_directory),
    ("POST", re.compile(r"/api/projects/validate"), projects.validate),
    ("GET", re.compile(r"/api/projects"), projects.recent),
    ("GET", re.compile(r"/api/agents/catalog"), catalog.get_catalog),
    ("GET", re.compile(r"/api/auth"), keys.auth_status),
    ("PUT", re.compile(r"/api/auth/([^/]+)"), keys.write_key),
    ("GET", re.compile(_PROJECT + r"/tasks"), tasks.task_list),
    ("GET", re.compile(_PROJECT + r"/tasks/([^/]+)"), tasks.task_detail),
    ("POST", re.compile(_PROJECT + r"/tasks"), tasks.create_task),
    ("PUT", re.compile(_PROJECT + r"/tasks/([^/]+)"), tasks.edit_task),
    ("GET", re.compile(_PROJECT + r"/commit"), tasks.commit_preview),
    ("POST", re.compile(_PROJECT + r"/commit"), tasks.commit),
    ("GET", re.compile(_PROJECT + r"/runs"), runs.list_runs),
    ("POST", re.compile(_PROJECT + r"/runs"), runs.start_run),
    ("POST", re.compile(_PROJECT + r"/runs/([^/]+)/open"), runs.open_run),
    ("POST", re.compile(_PROJECT + r"/runs/([^/]+)/resume"), runs.resume_run),
    ("DELETE", re.compile(_PROJECT + r"/live"), runs.stop_live),
)


def _route(method: str, path: str) -> tuple[Callable[[Request], BaseModel], tuple[str, ...]]:
    for route_method, pattern, handler in _ROUTES:
        match = pattern.fullmatch(path)
        if match is not None and route_method == method:
            return handler, tuple(unquote(group) for group in match.groups())
    message = f"no endpoint for {method} {path}"
    raise ApiError(ErrorCode.NOT_FOUND, message)


class HomeServer(ThreadingHTTPServer):
    """A loopback HTTP server bound to one ``HomeConfig``."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, config: HomeConfig) -> None:
        """Bind 127.0.0.1 on ``config.port`` (0 picks a free port and updates the config)."""
        super().__init__(("127.0.0.1", config.port), _Handler)
        config.port = int(self.server_address[1])
        self.config = config

    @property
    def url(self) -> str:
        """Return the capability URL that opens the app."""
        return f"{self.config.origin}/?token={self.config.token}"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    @override
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        _LOG.info("%s %s %s", self._command(), self._path(), code)

    @override
    def log_message(self, format: str, *args: object) -> None:
        # The raw request line carries the query (a capability token, possibly
        # percent-encoded), so only the method, the path, and the fixed format are logged.
        _LOG.info("%s %s: %s", self._command(), self._path(), format)

    @override
    def end_headers(self) -> None:
        # Every response, including send_error's, must carry the security headers.
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def _command(self) -> str:
        return getattr(self, "command", None) or "-"

    def _path(self) -> str:
        return urlsplit(getattr(self, "path", "") or "").path or "-"

    @property
    def _config(self) -> HomeConfig:
        return cast("HomeServer", self.server).config

    def _dispatch(self, method: str) -> None:
        try:
            self._handle(method)
        except ApiError as error:
            self._send_json(error.status, ErrorBody.of(error))
        except Exception:  # noqa: BLE001  # lint-waiver: LW-101301 [BLE001]; one failed request must answer 500 instead of dropping the connection.
            # > Catching only known types lets an unforeseen error close the socket
            # > with no response; a try block in every endpoint repeats this one.
            _LOG.exception("home request failed: %s %s", method, urlsplit(self.path).path)
            internal = ApiError(ErrorCode.INTERNAL, "internal error")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, ErrorBody.of(internal))

    def _handle(self, method: str) -> None:
        config = self._config
        self._require_host(config)
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if method == "GET" and parsed.path.startswith("/assets/"):
            self._send_asset(config, unquote(parsed.path.removeprefix("/")))
            return
        if method == "GET" and parsed.path in {"/", "/index.html", "/health"}:
            self._require_query_token(config, query)
            if parsed.path == "/health":
                self._send(HTTPStatus.OK, b"vibesys-ok\n", "text/plain")
            else:
                self._send_asset(config, "index.html")
            return
        if not parsed.path.startswith("/api/"):
            message = "not found"
            raise ApiError(ErrorCode.NOT_FOUND, message)
        self._require_bearer_token(config)
        if method in _STATE_CHANGING:
            self._require_origin(config)
        handler, params = _route(method, parsed.path)
        body = self._read_body() if method in {"POST", "PUT"} else b""
        result = handler(Request(config=config, params=params, query=query, body=body))
        self._send_json(HTTPStatus.OK, result)

    def _require_host(self, config: HomeConfig) -> None:
        # One allowlist: the Host of the exact origin the app is served from and Origin checks use.
        if self.headers.get("Host") != config.origin.removeprefix("http://"):
            message = "unexpected Host header"
            raise ApiError(ErrorCode.FORBIDDEN_ORIGIN, message)

    def _require_query_token(self, config: HomeConfig, query: dict[str, list[str]]) -> None:
        token = query.get("token", [""])[0]
        if not secrets.compare_digest(token.encode(), config.token.encode()):
            message = "missing or invalid capability token"
            raise ApiError(ErrorCode.UNAUTHORIZED, message)

    def _require_bearer_token(self, config: HomeConfig) -> None:
        header = self.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        if not secrets.compare_digest(token.encode(), config.token.encode()):
            message = "missing or invalid capability token"
            raise ApiError(ErrorCode.UNAUTHORIZED, message)

    def _require_origin(self, config: HomeConfig) -> None:
        if self.headers.get("Origin") not in {config.origin, *config.dev_origins}:
            message = "state-changing requests must come from the app origin"
            raise ApiError(ErrorCode.FORBIDDEN_ORIGIN, message)

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        if not raw_length.isdigit() or int(raw_length) > _MAX_BODY_BYTES:
            message = f"Content-Length must be an integer of at most {_MAX_BODY_BYTES} bytes"
            raise ApiError(ErrorCode.INVALID_REQUEST, message)
        return self.rfile.read(int(raw_length))

    def _send_asset(self, config: HomeConfig, relative: str) -> None:
        root = config.assets_dir
        if root is None:
            message = "web assets are not built; run `pnpm build` in clients/web"
            raise ApiError(ErrorCode.NOT_FOUND, message)
        allowed = root if relative == "index.html" else root / "assets"
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(allowed) or not candidate.is_file():
            message = "not found"
            raise ApiError(ErrorCode.NOT_FOUND, message)
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)

    def _send_json(self, status: HTTPStatus, model: BaseModel) -> None:
        body = model.model_dump_json(by_alias=True).encode()
        self._send(status, body, "application/json")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _settings_path(web_dir: Path) -> Path:
    return web_dir / "home-settings.json"


def saved_port(web_dir: Path) -> int:
    """Return the saved listen port, or the default."""
    try:
        saved = json.loads(_settings_path(web_dir).read_text(encoding="utf-8"))["port"]
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_PORT
    return saved if isinstance(saved, int) and 0 < saved < 1 << 16 else DEFAULT_PORT


def save_port(web_dir: Path, port: int) -> None:
    """Persist *port* as the default; call only after it bound."""
    atomic_write(_settings_path(web_dir), (json.dumps({"port": port}) + "\n").encode(), mode=0o600)


def _announce(url: str, *, open_browser: bool) -> None:
    print(f"VibeSys home: {url}", flush=True)  # noqa: T201  # lint-waiver: LW-101303 [T201]; the capability URL on stdout is the handoff to Electron and to the operator.
    # > Logging would route the URL through handlers that may be redirected or
    # > reformatted; the launcher parses this exact stdout line.
    if open_browser:
        webbrowser.open(url, new=2)


def run_home(args: argparse.Namespace, repository_root: Path) -> int:
    """Serve the app until interrupted; reuse a running home server instead of a second one."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    web_dir = state_home() / "web"
    record_path = web_dir / "home.json"
    existing = WebInstanceRecord.discover(record_path)
    if existing is not None:
        _announce(existing.url, open_browser=args.open)
        return 0
    claim = WebInstanceClaim(record_path)
    if not claim.try_acquire():
        message = "vibesys web home: another home server is starting"
        raise SystemExit(message)
    assets = args.assets or repository_root / "clients" / "web" / "dist"
    config = HomeConfig(
        state_home=web_dir.parent,
        roots=tuple(root.expanduser().resolve() for root in args.root) or (Path.home().resolve(),),
        dotenv_path=DOTENV_PATH,
        assets_dir=assets.resolve() if assets.is_dir() else None,
        port=args.port or saved_port(web_dir),
        dev_origins=tuple(args.dev_origin),
    )
    try:
        server = HomeServer(config)
    except OSError as error:
        claim.close()
        message = (
            f"vibesys web home: cannot listen on {config.origin} ({error.strerror}). "
            "Free the port, or pass --port; a new port changes the app origin, and run "
            "gateways started for the old one reject the app until reopened."
        )
        raise SystemExit(message) from None
    previous = saved_port(web_dir)
    if config.port != previous:
        save_port(web_dir, config.port)
        for gateway in runs.origin_mismatches(config):
            _LOG.warning("gateway %s allows only the old origin; stop and reopen it", gateway)
    record = WebInstanceRecord.from_gateway(
        pid=os.getpid(), port=config.port, token=config.token, project_root=web_dir
    )
    record.write(record_path)
    _announce(server.url, open_browser=args.open)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        record.remove_if_owner(record_path)
        claim.close()
    return 0
