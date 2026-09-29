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
real-project, and remote-host workflows.

Without a gateway (`vibesys web dev`, or `pnpm --filter @vibesys/web dev`),
the page replays `src/fixtures/demo-run.jsonl` in the browser and offers a
form to connect to a live gateway URL.

## Behavior

- Three regions: a round rail, the selected round's log, and an inspector.
  From 768 to 1199 px the inspector is a dialog opened by selecting a round.
  Below 768 px the rail is a chip strip, and tapping the selected chip opens
  the inspector as a bottom sheet.
- Core-state projects snapshots, ordered events, duplicate replay, execution
  checkpoints, and history prefixes. Bootstrap requests 300 events. When the
  selected round starts below that tail, earlier history loads on its own in
  500-event chunks until the round's start is in.
- `query.snapshot`, `query.experiments`, `query.design`, and
  `query.performance` run once per bootstrap batch; the last three rerun on
  `experiments_changed`. Only `performance_context` is kept: the header
  objective and the R0 baseline.
- Pause and Resume follow the run status only. A failed command shows its
  diagnostic next to the control until the next command. A steer shows as
  Queued until a `control` consumed event places it in the log.
- Keys: `j`/`k` or the arrows change the round, `p` pauses or resumes, `/`
  focuses the steer input, and `?` lists the keys with a toggle that turns
  the single-key shortcuts off or back on.

## Verification

```sh
pnpm --dir clients --filter @vibesys/web check
pnpm --dir clients --filter @vibesys/web test
pnpm --dir clients --filter @vibesys/web build
pnpm --dir clients --filter @vibesys/web test:e2e
CAPTURE_DIR=/tmp/vs-web-frames pnpm --dir clients --filter @vibesys/web exec playwright test screens
```

`e2e/gateway.ts` mocks the gateway's `/ws` socket over `src/fixtures/demo-run.jsonl`.
`app.spec.ts` checks behaviour; `screens.spec.ts` writes each screen at 1440 and 1024
in dark and light when `CAPTURE_DIR` is set. Review every PNG by eye.
