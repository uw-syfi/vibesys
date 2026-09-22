# VibeSys web workspace

Single-run React workspace for watching and steering a live run, using
`@vibesys/backend-client/browser` and `@vibesys/core-state`.

## Run locally

From the repository root:

```sh
uv sync
pnpm install
pnpm --filter @vibesys/web build
```

Follow the [gateway startup guide](../../src/server/README.md) to start the
gateway and backend on the same control socket. Then run:

```sh
pnpm --filter @vibesys/web dev
```

Open <http://127.0.0.1:5173>. Vite binds loopback and proxies `/api/*` to
`http://127.0.0.1:8765`, preserving the browser origin. Set
`VIBESYS_GATEWAY_URL` when the gateway uses another port; also add the UI
origin with the gateway's `--dev-origin` flag if its port changes.

For the built application, stop the dev server and run
`pnpm --filter @vibesys/web preview`. Preview uses the same port and proxy.
The gateway serves API routes only. Candidate files are never served.

## Behavior

- Three regions: a round rail, the selected round's log, and an inspector.
  From 768 to 1023 px the inspector is a dialog opened by selecting a round.
  Below 768 px the rail is a chip strip, and tapping the selected chip opens
  the inspector as a bottom sheet.
- Core-state projects snapshots, ordered events, duplicate replay, execution
  checkpoints, and history prefixes. Bootstrap requests 300 events. When the
  selected round starts below that tail, earlier history loads on its own in
  500-event chunks until the round's start is in.
- `query.snapshot`, `query.experiments`, `query.design`, and
  `query.performance` run once per bootstrap batch; the last three rerun on
  `experiments_changed`. Nothing polls, because every query but the snapshot
  appends a journal event that wakes every subscriber. Only
  `performance_context` is kept: the header objective and the R0 baseline.
- Pause and Resume follow the run status only; Pausing stays disabled until
  the pause lands. A failed command shows its diagnostic next to the control
  until the next command. A steer shows as Queued until a `control` consumed
  event places it in the log.
- Keys: `j`/`k` or the arrows change the round, `p` pauses or resumes, `/`
  focuses the steer input, and `?` lists the keys with a toggle that turns
  the single-key shortcuts off or back on.

## Verification

```sh
pnpm --filter @vibesys/web check
pnpm --filter @vibesys/web test
pnpm --filter @vibesys/web build
pnpm exec playwright install chromium
pnpm test:web-browser
node clients/web/scripts/capture.mjs --out /tmp/vs-web-frames --strict
```

The tests cover the session (replay, reconnect, backfill, query budget,
control capture, commands) and the pure derivations in `src/derive.ts`
against recorded runs. `pnpm test:web-browser` drives a deterministic real
backend through the gateway: pause, steer, resume, reload, completion, and
the phone layout. On mocked runs it checks round selection, run replacement,
live follow, focus, tooltips, the drawer, and the composer. The capture
script serves `dist` with `vite preview`, replays
`clients/tui/dev/fixtures/queue-rs-payloads.jsonl` and
`src/fixtures/stub-run.jsonl` through a mocked gateway, and writes each state
at 1440, 1024, and 390 px in dark and light. `--live <url>` drives a running
gateway instead. A PASS checks text and layout overflow only; review the
frames by eye.
