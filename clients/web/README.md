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

This command invokes the browser entrypoint directly. It does not start the
OpenTUI client. The replay gateway remains detached until explicitly stopped;
use the printed instance-record path to stop it:

```bash
uv run python -m entrypoints.web stop --instance /path/from/output.json
```

See [Web UI development](../../docs/contributing/web-development.md) for replay,
real-project, and remote-host workflows. In an SSH session the script does not
try to open a browser on the remote host; forward port 8765 and open the printed
capability URL on the local machine.
