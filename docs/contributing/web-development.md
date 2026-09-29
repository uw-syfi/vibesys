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

Open `http://127.0.0.1:5173`. This mode replays `clients/web/src/fixtures/demo-run.jsonl` in the
browser; queries answer empty, so round titles come from the recorded plans. It starts no Python
server. Append `?gateway=<capability URL>` to connect the page to a live gateway instead.

## Home mode

Start the home server with the Vite origin allowed, then open the Vite page with its token:

```bash
uv run python -m entrypoints.web home --dev-origin http://127.0.0.1:5173
```

It prints `VibeSys home: http://127.0.0.1:8764/?token=<token>`. Open
`http://127.0.0.1:5173/?token=<token>`: Vite proxies `/api` to the home server
(`VIBESYS_HOME_PORT`, default 8764), so New run, keys and runs use the real API with hot reload.

## Live demo mode

The demo reopens the repository's recorded run through the real HTTP and
WebSocket gateway. The bundle is the event journal
`clients/web/src/fixtures/demo-run.jsonl` plus the recorded project
`clients/web/src/fixtures/demo-project`, so experiment and performance queries
return the recorded data:

```bash
uv run python -m entrypoints.web live --demo --open
```

The command builds `clients/web`, starts a detached loopback gateway on port
8765, and prints a capability-bearing URL. Open that URL in a browser. The
gateway remains available until explicitly stopped, so the browser can inspect
the completed state without an agent CLI or credentials.

Use `status` and `stop` with the instance path printed by the command when the
gateway needs to be inspected or stopped. For `--demo`, the instance record is
created in a temporary directory, so use the printed `Instance record` path
rather than a path under the repository checkout:

```bash
uv run python -m entrypoints.web status --instance /path/from/Instance-record-output.json
uv run python -m entrypoints.web stop --instance /path/from/Instance-record-output.json
```

Pass a real project and task for an operator-owned run. Additional VibeSys run
arguments follow `--`:

```bash
uv run python -m entrypoints.web live --project /path/to/project --task TASK --open -- --outer-loop agent --local
```

## Remote host and local laptop

The gateway intentionally binds only to loopback. Start it on the remote host
with a fixed port and the SSH target that you will use from your laptop:

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

## Home server

`uv run python -m entrypoints.web home` serves the desktop app and its setup API on
`127.0.0.1:8764` and prints `VibeSys home: <capability URL>`. Pass `--port` once to change
the saved port, `--root DIR` (repeatable) to limit the folder picker (default: your home
directory), and `--dev-origin http://127.0.0.1:5173` when the Vite dev server proxies `/api`.
The API contract is `src/entrypoints/web_home/contract.py`; print its JSON Schema with
`uv run python -m entrypoints.web_home.contract`. Keys saved from the app go to the
checkout's `.env` (mode 0600); the server never loads that file into its own environment.

## Desktop app

`clients/desktop` is an Electron shell. It starts `vibesys web home` from this checkout and
opens the app in a native window. Run these from `clients/`:

| Command | What runs |
| --- | --- |
| `pnpm desktop` | Vite with hot reload on `http://127.0.0.1:5173`, and the home server with `--dev-origin` for it. Edits under `clients/web` reload in place; main-process and preload edits need a restart. A home server that was already running without `--dev-origin` is reused, and writes from Vite fail until it is restarted. |
| `pnpm desktop:start` | Builds `clients/web` and the shell, then loads the built app from the home server. |
| `pnpm --filter @vibesys/desktop test:e2e` | Launches the built app with Playwright on a temporary state home and screenshots the window. Needs `uv`. |

`VIBESYS_HOME_PORT` sets the home server's port (the Vite proxy reads it too).
`VIBESYS_STATE_HOME` moves the home server's state and the shell's profile; one app runs per
state home. Quitting stops the home server the app started; run servers keep running and are
listed again on the next launch. A home server that was already running (for example
`vibesys web home --open`) is reused and left running. There is no packaged build yet.
