# Web UI development

The web UI has two supported local workflows. Use replay mode when working on
React presentation and use live mode when checking the browser transport,
server gateway, and shared state projection.

## One-command local browser demo

From the repository root, run:

```bash
./scripts/run-web-ui.sh
```

This is the browser-only source-checkout launcher. It installs the locked
JavaScript workspace, builds `clients/web`, starts a detached live gateway on
`127.0.0.1:8765`, replays a recorded run through the real WebSocket protocol,
and asks the host to open the capability URL in its default browser. If the
browser cannot be opened automatically, open the `VibeSys web UI ready` URL
printed in the terminal.

The script directly invokes `entrypoints.web`; it does not invoke the
`vibesys` launcher or start OpenTUI. The `vibesys` executable remains the
interactive TUI launcher by default.

## Replay mode

From a VibeSys source checkout:

```bash
uv run python -m entrypoints.web dev
```

Open `http://127.0.0.1:5173`. This mode serves the deterministic event fixture
through Vite and shows a `Live gateway URL` field. It does not start a Python
server or a WebSocket connection until a capability URL is submitted.

## Live demo mode

The demo serves the repository's deterministic recorded run through the real
HTTP and WebSocket gateway:

```bash
uv run python -m entrypoints.web live --demo --open
```

The command builds `clients/web`, starts a detached loopback gateway on port
8765, and prints a capability-bearing URL. Open that URL in a browser. The
gateway remains available until explicitly stopped, so the browser can inspect
the completed state without an agent CLI or credentials. A second invocation
from the same checkout reuses that gateway instead of competing for port 8765.

Use `status` and `stop` with the instance path printed by the command when the
gateway needs to be inspected or stopped. For `--demo`, the ignored runtime
directory is stable within the source checkout:

```bash
uv run python -m entrypoints.web status --instance clients/web/.vibesys-demo/web-gateway.json
uv run python -m entrypoints.web stop --instance clients/web/.vibesys-demo/web-gateway.json
```

Pass a real project and task for an operator-owned run. Additional VibeSys run
arguments follow `--`:

```bash
uv run python -m entrypoints.web live --project /path/to/project --task TASK --open -- --outer-loop agent --local
```

## Gateway HTTP hygiene

Every gateway HTTP response, including the 403 and 404 bodies, carries the same
three headers from one writer (`_HYGIENE_HEADERS` in
`src/server/transport/websocket.py`), so no route can set or duplicate them:

| Header | Value | Reason |
| --- | --- | --- |
| `Cache-Control` | `no-store` | The page URL carries the capability token and run output is live. |
| `Referrer-Policy` | `no-referrer` | A `Referer` header would copy that token to any navigation target. |
| `Content-Security-Policy` | below | Transcripts render model- and tool-produced text. |

The policy denies every fetch destination and then grants only what the built
bundle uses:

```text
default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self';
font-src 'none'; connect-src 'self' ws://127.0.0.1:*; base-uri 'none';
form-action 'none'; frame-ancestors 'none'; object-src 'none'
```

`script-src` and `style-src` are `'self'` with no `'unsafe-inline'`: the Vite
production build emits one external script and one external stylesheet, and the
React tree sets no inline `style` attributes, so nothing inline is needed.
`connect-src` names `ws://127.0.0.1:*` instead of relying on `'self'` because
`'self'` is not resolved against `ws:` by every browser engine. Its port is
wildcarded because the bound port is only known after startup while this policy
is one value shared by every response; the WebSocket handshake Origin check is
what pins the socket to this gateway instance, and `connect-src` only has to
keep an injected exfiltration channel on loopback. `font-src 'none'` keeps the
bundle usable on an air-gapped host.

`clients/web/e2e/gateway-hygiene.spec.ts` asserts the whole policy against a
live gateway, fails on any reported `securitypolicyviolation`, and aborts and
fails on any request the page makes off loopback.

### `/assets/*` is served without a capability token

`_process_request` requires the token on every path except `/assets/*`, and that
exemption is deliberate rather than an oversight. The token lives in the page
URL, and a subresource request carries no query string of its own, so requiring
it would mean rewriting every asset URL in the built `index.html` at serve time
or moving the token into a cookie. The assets are the public frontend bundle and
hold no run data. Run data moves only over `/ws`, which requires both the token
and an exact Origin match. Nothing else is exempt, `/health` included: the only
thing that probes `/health` is record discovery in
`src/server/transport/discovery.py`, which has already read the instance record
and therefore already holds the token, so the reason for the exemption does not
apply to it.

The exemption is a statement about the URL space, not about the filesystem, so
it only holds if the path that satisfies the `/assets/` test is the same path
that is looked up. `_routing_path` percent-decodes and normalizes the request
target once, and `_process_request` derives both the token decision and the
asset lookup from that single value. A path such as `/assets/../index.html`
therefore normalizes to `/index.html` before either decision is made, so it
requires the token like any other page request. Deciding the exemption on the
raw target and then looking the file up from a separately decoded copy would
widen the exemption from "URLs under `/assets/`" to "anything reachable under
the assets directory", which is not what this decision grants.

## Remote host and local laptop

The gateway intentionally binds only to loopback. For the one-command demo,
create the tunnel when connecting from the laptop:

```bash
ssh -L 8765:127.0.0.1:8765 USER@chelan3
```

In that remote shell, run:

```bash
cd /path/to/VibeSys
./scripts/run-web-ui.sh --port 8765
```

The script detects the SSH session and does not try to open a browser on the
remote host. Open its printed `VibeSys web UI ready` URL in the laptop browser;
the existing SSH connection forwards both HTTP and WebSocket traffic.

For the separate Vite browser harness workflow, start the gateway on the remote
host with a fixed port and the SSH target that you will use from your laptop:

```bash
uv run python -m entrypoints.web live --demo --port 8765 --ssh-target USER@chelan3
```

On the laptop, start the browser harness and run the printed tunnel command in
separate terminals:

```bash
uv run python -m entrypoints.web dev --port 5173
ssh -N -L 8765:127.0.0.1:8765 USER@chelan3
```

The remote command must allow the browser harness origin:

```bash
uv run python -m entrypoints.web live --demo --port 8765 --browser-origin http://127.0.0.1:5173 --ssh-target USER@chelan3
```

Open the `Browser harness URL` printed by the remote command after starting
the tunnel. The browser page is served locally, and its WebSocket connection
is forwarded to the remote gateway.

The tunnel helper is equivalent when the capability URL is already available:

```bash
uv run python -m entrypoints.web tunnel --host USER@chelan3 --url 'http://127.0.0.1:8765/?token=TOKEN'
```

Open the `Open locally` URL printed by the helper. The local and remote ports
must match because the gateway checks the exact browser Origin during the
WebSocket handshake. The browser harness origin must be explicitly supplied
with `--browser-origin`. The capability token is kept in the URL and is never
passed as an SSH argument.

The remote project record is project-local and can be reused by a second launch
without creating another gateway. Stop the remote gateway from the remote host
with the `stop` command, or send `SIGTERM` to the recorded PID.
