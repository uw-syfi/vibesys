# Web UI development

The web UI has two supported local workflows. Use replay mode when working on
React presentation and use live mode when checking the browser transport,
server gateway, and shared state projection.

## Replay mode

From a VibeSys source checkout:

```bash
uv run vibesys web dev
```

Open `http://127.0.0.1:5173`. This mode serves the deterministic event fixture
through Vite and shows a `Live gateway URL` field. It does not start a Python
server or a WebSocket connection until a capability URL is submitted.

## Live demo mode

The demo uses the repository's queue example and a deterministic local agent:

```bash
uv run vibesys web live --demo
```

The command builds `clients/web`, starts a detached loopback gateway on port
8765, and prints a capability-bearing URL. Open that URL in a browser. The
gateway remains available after the one-round demo finishes, so the browser
can inspect the completed state.

Use `status` and `stop` with the instance path printed by the command when the
gateway needs to be inspected or stopped:

```bash
uv run vibesys web status --instance examples/data-structures/repositories/queue-rs/.vibesys/web-gateway.json
uv run vibesys web stop --instance examples/data-structures/repositories/queue-rs/.vibesys/web-gateway.json
```

Pass a real project and task for an operator-owned run. Additional VibeSys run
arguments follow `--`:

```bash
uv run vibesys web live --project /path/to/project --task TASK -- --outer-loop agent --local
```

## Remote host and local laptop

The gateway intentionally binds only to loopback. Start it on the remote host
with a fixed port and the SSH target that you will use from your laptop:

```bash
uv run vibesys web live --demo --port 8765 --ssh-target USER@chelan3
```

On the laptop, start the browser harness and run the printed tunnel command in
separate terminals:

```bash
uv run vibesys web dev --port 5173
ssh -N -L 8765:127.0.0.1:8765 USER@chelan3
```

The remote command must allow the browser harness origin:

```bash
uv run vibesys web live --demo --port 8765 --browser-origin http://127.0.0.1:5173 --ssh-target USER@chelan3
```

Open the `Browser harness URL` printed by the remote command after starting
the tunnel. The browser page is served locally, and its WebSocket connection
is forwarded to the remote gateway.

The tunnel helper is equivalent when the capability URL is already available:

```bash
uv run vibesys web tunnel --host USER@chelan3 --url 'http://127.0.0.1:8765/?token=TOKEN'
```

Open the `Open locally` URL printed by the helper. The local and remote ports
must match because the gateway checks the exact browser Origin during the
WebSocket handshake. The browser harness origin must be explicitly supplied
with `--browser-origin`. The capability token is kept in the URL and is never
passed as an SSH argument.

The remote project record is project-local and can be reused by a second launch
without creating another gateway. Stop the remote gateway from the remote host
with the `stop` command, or send `SIGTERM` to the recorded PID.
