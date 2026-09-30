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

`stop` exits 0 only once no process has files open under the instance
directory, so after it succeeds that directory can be reused or removed. It
reaches that conclusion from the gateway's lock on its startup log, not from
the instance record, which the gateway unlinks as the first step of teardown
and which therefore goes away while the gateway is still running. `stop` waits
up to 10 seconds and exits 1 if the files are still open then, naming the pid
it signalled; do not reuse or remove the directory after that, and escalate
with `kill -9` yourself if `status` shows a wedged gateway. `stop` sends only
SIGTERM, because that is what runs the gateway's ordered teardown. The claim
lock and the startup log are retained by design; `stop` does not delete them.

Pass a real project and task for an operator-owned run. Additional VibeSys run
arguments follow `--`:

```bash
uv run python -m entrypoints.web live --project /path/to/project --task TASK --open -- --outer-loop agent --local
```

## Gateway HTTP hygiene

Every gateway HTTP response, including the 403 and 404 bodies, is built by
`WebSocketGateway._response` in `src/server/transport/websocket.py`, which
merges the headers into one dict literal, so no route can set or duplicate one:

| Header | Value | Reason |
| --- | --- | --- |
| `Cache-Control` | `no-store` | The page URL carries the capability token and run output is live. |
| `Referrer-Policy` | `no-referrer` | A `Referer` header would copy that token to any navigation target. |
| `X-Content-Type-Options` | `nosniff` | `/assets/*` is token-free and the content type falls back to `application/octet-stream`, so no response may be re-typed by sniffing. |
| `Content-Security-Policy` | derived | Transcripts render model- and tool-produced text. |

`_POLICY_DIRECTIVES` in that module is the authoritative directive map, and
`_content_security_policy` is its only serializer. Do not add another copy of
the header value. Two exist on purpose, as independent cross-checks that fail
when they disagree with the module: `_expected_policy` in
`tests/server/test_websocket_transport.py` and the `expectedPolicy` function in
`clients/web/e2e/gateway-hygiene.spec.ts`. Neither imports the gateway, so
neither agrees by construction. The directive values named below are
explanation and not a third copy, since nothing reads them. The shape is
deny-everything (`default-src 'none'`) plus these grants:

- `script-src 'self'` and `style-src 'self'`, no `'unsafe-inline'`. The Vite
  production build emits one external script and one external stylesheet. What
  the policy actually forbids is a `<style>` element and
  `setAttribute('style', ...)`; it does not govern CSSOM writes, so React's
  `style={{...}}` prop (which compiles to `el.style.setProperty`) is unaffected.
  The real constraints on a contributor are therefore: no CSS-in-JS runtime
  (emotion and styled-components inject `<style>`) and no React 19
  `<style href precedence>` hoisting.
- `img-src 'self'`. The app renders no images, but Chromium enforces `img-src`
  on the favicon it probes for on its own, and a denied probe is a reported
  `securitypolicyviolation` that fails the browser audit. The grant allows
  nothing off-origin, and it does not make `/favicon.ico` reachable: that path
  requires the capability token like any other non-`/assets/` path and answers
  403.
- `font-src 'none'`, stated rather than left to the `default-src` fallback,
  because "no external font" is the air-gap property being asserted.
- `connect-src 'self'` plus this gateway's own WebSocket authority. `'self'` is
  separately load-bearing and must not be removed: Vite's modulepreload
  polyfill `fetch`es `link[rel=modulepreload]` hrefs, and a `fetch` of a script
  URL is governed by `connect-src`, not `script-src`.

`connect-src` names exactly one socket source, `ws://127.0.0.1:<this gateway's
bound port>`. That is the authority of every page this gateway hands out
(`WebSocketGateway.url` is always `http://127.0.0.1:<bound port>/?token=...`),
so the grant is exact and textual.

**Declared browser origins do not appear in `connect-src`, and must not be
added to it.** They answer a different question. `--web-origin` is the inbound
`Origin` allowlist: it says who may connect *to* the gateway. `connect-src`
says where a page the gateway *served* may connect to. Neither documented flow
makes a declared origin an answer to the second one:

- In the development flow the page is served by Vite, so it carries Vite's
  policy and the gateway's header never governs it.
- In a proxied deployment the page is at the proxy's authority and its socket
  uses the proxy's own port, never the gateway's.

Three shapes this replaced were each wrong in a different direction, which is
why the current rule is narrow. A wildcard port (`ws://127.0.0.1:*`) authorized
a socket to any other loopback listener, including another user's on a shared
host. Taking each declared origin's full authority kept that grant for every
declared port, including the `--browser-origin http://127.0.0.1:5173` this
document uses as the normal case. Pairing each declared *hostname* with the
gateway's bound port was wrong in both directions at once: it named authorities
no page can ever be served from (`ws://proxy.example:<gateway port>` when the
page needs the proxy's port), and because a CSP `host-part` admits a bare `*`
(`host-char = ALPHA / DIGIT / "-"`, so `*` is valid and `[::1]` is not),
`--web-origin http://*:5173` put `ws://*:<port>` in the header and authorized
the capability token to any host at that port, while `--web-origin
'http://!;evil.example:5173'` split the directive with its `;` and dropped the
gateway's own socket source.

The value is now built from module constants and the bound socket only, so no
configured string reaches a security header at all. Declared origins are
instead parsed and canonicalized when they enter, by `browser_origin` in
`src/server/transport/websocket.py`, which is the one definition of "an origin
a browser can send" and is what both `--web-origin` and the gateway
constructor use. It rejects anything a browser could not send, naming the
offending value, and lowercases the scheme and host, compresses an IPv6
literal, and drops a default port, because the handshake compares `Origin` by
exact string and `http://LOCALHOST:5173`, `http://proxy.example:80`, and
`http://[0:0:0:0:0:0:0:1]:5173` would otherwise be allowlist entries that never
match. Note this predicate is deliberately *not* the CSP `host-part` grammar:
`http://[::1]:5173` is a legitimate `Origin` and is accepted, even though
`[::1]` is not a valid CSP `host-part`.

The residual, stated rather than claimed away: a page reached by some other
spelling of the same socket (`http://localhost:<port>`), or served through a
proxy that forwards these headers, is not covered by the explicit source and
falls back on `'self'`. CSP3 states `'self'` covers the `wss:` variant of the
page's origin but is less clear about plain `ws:`. Deriving the source from the
document request's own authority would close that exactly, and is the intended
follow-up; no declared origin can close it, which is the point above. The bound
port being unknown before `start()` is not an obstacle: `_authority` falls back
to the requested `port`, and the policy is computed per response.

The narrowed header is defense in depth and not a fix for the client. The page
still builds its own socket URL: `webSocketUrlFromLocation` in
`clients/web/src/session.ts` takes the socket authority from the page's
`?gateway=` query parameter while inheriting the page's `?token=`, so a link
carrying both still asks the browser to send this gateway's token to another
authority. The header now refuses that for a foreign port, but the client
should not be constructing it at all; that is #1041.

`clients/web/e2e/gateway-hygiene.spec.ts` asserts the whole policy against a
live gateway, fails on any reported `securitypolicyviolation` (with a negative
control that proves the listener fires), and blocks plus fails on any request or
WebSocket the page attempts off loopback.

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

The filesystem lookup is rooted at the same subtree for the same reason.
`/assets/*` is resolved under `<dist>/assets` and the containment guard is
checked against that directory rather than against the dist root above it, so a
symlink planted inside `assets/` that points elsewhere under the dist root is a
404 and not a token-free file. Rooting the guard at the dist root would again
make the exemption a filesystem statement.

Normalizing once also makes trailing slashes and `.` segments equivalent to
their normalized target, which is a deliberate widening rather than a side
effect: `/health/`, `/index.html/`, `/./`, `/..`, and `/ws/` route as
`/health`, `/index.html`, `/`, `/`, and `/ws`, where each of those spellings
was a 404 before. It applies inside `/assets/` too: `/assets/index.js/` and
`/assets/index.js/.` are the same token-free target as `/assets/index.js`,
while `/assets/index.js/..` normalizes to `/assets` and therefore leaves the
exemption. Accepted because one normalization is what makes the `/assets/` test
mean what it says, and because every route keeps its own check under every
spelling: `/ws/` still requires both the capability token and an exact Origin
match, and a target above the root resolves to the token-required index rather
than outside the tree. The whole table is pinned in
`tests/server/test_websocket_transport.py`.

The target is split on its first `?` rather than parsed as a URL, because an
origin-form request target is `absolute-path [ "?" query ]` (RFC 9110 7.1) and
not a URL. Two consequences, both pinned: `#` is an ordinary query byte and so
is part of the token rather than a fragment delimiter, and a target no URL
grammar accepts is still answered. `urlsplit` raised `ValueError` on a
malformed IPv6 authority (`//[::/`, or any absolute-form target with one),
which left the handshake through the `websockets` library's own error path as
an unauthenticated 500 carrying none of these headers. The capability token is
compared as bytes for the same reason, encoded with `surrogateescape`: the
library decodes the target with `ascii`/`surrogateescape`, so a raw byte above
0x7F arrives as a lone surrogate that strict UTF-8 refuses, just as
`compare_digest` refuses a non-ASCII `str`.

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
