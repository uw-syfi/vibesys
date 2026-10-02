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

Without a gateway (`vibesys web dev`, or `pnpm --filter @vibesys/web dev`), the
page replays `src/fixtures/demo-run.jsonl` in the browser. `?gateway=<url>`
points the same page at a live gateway, whose URL must carry its own capability
token: the page never forwards its own.

## Behavior

- One window: a sidebar (projects, runs, and the open run's rounds), the selected round's
  transcript, and an optional right pane (Ask, Changes, Agents, Experiments, Notes). The sidebar
  yields before the transcript drops below 560 px; both edges resize by drag or arrow keys.
- The title row says what the run is doing now, offers Pause or Resume (hidden while a
  transition is pending), shows the kept checkpoint against the baseline, toggles the pane, and
  opens ••• (Notes, Copy run ID, Theme, Stop run with a confirmation).
- The transcript groups a round by agent execution: one-line tool calls that open to their
  output or diff, Prompt and Todos when the execution recorded them, and the judge verdict. A
  steer shows Queued on the backend's pending acknowledgment and Applied on its consumed control
  event.
- Ask talks to the run's experiment chat: a thread switcher, New thread, and a model chip whose
  choices come from the run's `query.chat_options` (a model belongs to a thread, so choosing one
  starts a thread on it). One question per thread is in flight at a time. Without a chat harness
  the tab says so and keeps recorded threads readable.
- Notes are private run notes shared with the TUI through the home server
  (`GET/PUT /api/notes/{run}`, last write wins), saved as you type. "Use as steer draft" and
  "Use as ask draft" only fill a composer; nothing is sent.
- Agents draws the round's executions top to bottom (React Flow, dagre). Edges follow start
  times and are dashed because events carry no dependency graph; a card filters the transcript.
- ⌘K lists the visible controls, the rounds and the pane tabs. The sidebar reads the home
  server's API; a replay page or a gateway's own page lists only the open run.
- Themes: System (default), Light and Dark, from ••• (run and home windows) or ⌘K, remembered
  per browser; `?theme=light|dark` overrides for reviews and captures.
- Session, core-state, the tail bootstrap and backfill are unchanged (`src/session.ts`).
- The home page (the URL `vibesys web home` prints) lists every recent project's runs. New run
  (⌘N) checks a folder, picks a task or creates one (its files are committed only after a
  confirmation), sets rounds, model, per-role models and advanced options, saves provider keys
  write-only to `.env`, and lists what blocks Start, each linked to its field. Start waits until
  the run attaches, then opens it; a failed start shows the run server's stderr with copyable
  file locations and Retry.
- A finished run reopens read-only from the sidebar; ••• Resume run… resumes it with its
  recorded configuration and an optional larger budget.

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
`e2e/live-gateway.ts` boots a real detached gateway for the specs that need one
(`live.spec.ts`, `gateway-hygiene.spec.ts`, `controls-banner.spec.ts`); those
run against a `vite build` output, so rebuild before running them.
