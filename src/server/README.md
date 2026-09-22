# Browser gateway

The gateway adapts HTTP and WebSocket traffic to the existing Unix JSONL
server. Backend protocol models remain authoritative. Requests are validated
and serialized onto the control socket; responses and subscription messages
pass through unchanged, including replay floors and execution checkpoints.

## Startup

From the repository root, install dependencies:

```sh
uv sync
pnpm install
pnpm --filter @vibesys/web build
```

Run the gateway in one terminal. It retries until the backend socket exists:

```sh
uv run python -m entrypoints.browser_gateway --control-socket /tmp/vibesys-web.sock
```

In another terminal, start the backend with your normal run arguments:

```sh
uv run python -m entrypoints.server --control-socket /tmp/vibesys-web.sock --input /absolute/path/to/input
```

The server waits up to 30 seconds for its first subscriber, so start the
gateway first. Keep the Unix socket path short on macOS. For an existing
backend, use its control socket instead of starting another run.

Start the browser UI in a third terminal:

```sh
pnpm --filter @vibesys/web dev
```

Open <http://127.0.0.1:5173>. Vite proxies `/api/request` and `/api/events`
to `http://127.0.0.1:8765`. For a built preview, replace `dev` with `preview`.

## Lifetime and boundaries

- The gateway holds a standing upstream subscription. Refreshing or closing
  browser tabs preserves the backend run. Each tab has an independent replay cursor.
- The gateway owns its subscriptions, not the backend process. Stop the backend
  explicitly when finished. After a run ends, its server can remain available
  for inspection until the gateway and other subscribers disconnect.
- `--host` accepts only loopback addresses; `--port` defaults to `8765`.
  The default origin allowlist includes localhost and 127.0.0.1 on gateway
  and Vite port `5173`. `--dev-origin ORIGIN` adds an explicit origin.
- Browser origins outside the allowlist receive HTTP 403, including WebSocket
  upgrades. No wildcard CORS is enabled. Headerless local CLI clients are accepted.
- The gateway serves only two API routes, never candidate files or UI assets.
  This is a local development interface, without remote authentication.

## Deterministic verification

```sh
uv run pytest --no-cov tests/server/test_browser_gateway.py
pnpm exec playwright install chromium
pnpm test:web-browser
```

The browser smoke test starts the real server runtime, gateway, and built UI
on temporary local endpoints. A deterministic invocation loop replaces paid
agent calls. It checks streamed output, control commands, refresh replay,
readiness, responsive layout, and keyboard focus, then stops its processes.
