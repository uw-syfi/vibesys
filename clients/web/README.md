# VibeSys web UI

From the repository root, start the complete local browser demo with one
command:

```bash
./scripts/run-web-ui.sh
```

The script installs the locked JavaScript workspace, builds this React client,
starts the live loopback WebSocket gateway on port 8765, replays a recorded run
through the real server protocol, and asks the host to open the capability URL
in its default browser. If the browser cannot be opened automatically, open the
`VibeSys web UI ready` URL printed in the terminal.

The browser exchanges that launch URL for an HttpOnly session cookie and
immediately removes the token from its address bar and WebSocket URLs. Rotate a
copied launch URL without interrupting attached pages with:

```bash
uv run python -m entrypoints.web rotate --instance clients/web/.vibesys-demo/web-gateway.json
```

This command invokes the browser entrypoint directly. It does not start the
OpenTUI client. Re-running the command reuses the same live demo gateway. The
gateway remains detached until explicitly stopped; its stable instance record
is `clients/web/.vibesys-demo/web-gateway.json`:

```bash
uv run python -m entrypoints.web stop --instance clients/web/.vibesys-demo/web-gateway.json
```

See [Web UI development](../../docs/contributing/web-development.md) for replay,
real-project, and remote-host workflows. In an SSH session the script does not
try to open a browser on the remote host; forward port 8765 and open the printed
capability URL on the local machine.
