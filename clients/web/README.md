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

- One window: a sidebar (projects, runs, and the open run's rounds), the selected round's
  transcript, and an optional right pane (Ask, Changes, Agents, Experiments, Notes). The sidebar
  yields before the transcript drops below 560 px; both edges resize by drag or arrow keys.
- The title row says what the run is doing now, offers Pause or Resume (hidden while a
  transition is pending), shows the kept checkpoint against the baseline, toggles the pane, and
  opens ••• (Notes, Copy run ID, Stop run with a confirmation).
- The transcript groups a round by agent execution: one-line tool calls that open to their
  output or diff, Prompt and Todos when the execution recorded them, and the judge verdict. A
  steer shows Queued on the backend's pending acknowledgment and Applied on its consumed control
  event.
- Agents draws the round's executions top to bottom (React Flow, dagre). Edges follow start
  times and are dashed because events carry no dependency graph; a card filters the transcript.
- ⌘K lists the visible controls, the rounds and the pane tabs. Ask and Notes are placeholders
  until sub-project 5. The sidebar reads `HomeApi`, a fixture until the home server exists.
- Themes: System (default), Light and Dark (`?theme=light|dark`).
- Session, core-state, the tail bootstrap and backfill are unchanged (`src/session.ts`).

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
