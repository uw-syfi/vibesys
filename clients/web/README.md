# VibeSys web workspace

Single-run React workspace using `@vibesys/backend-client/browser` and
`@vibesys/core-state`.

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

- Hypotheses and rounds filter activity. Navigation and the inspector expand
  above content on narrow screens. Controls use native keyboard interactions.
- Core-state projects snapshots, ordered events, duplicate replay, execution
  checkpoints, and history prefixes. Bootstrap requests 300 events; earlier
  history loads in 500-event chunks, excluding already replayed spine events.
  The transcript renders the newest 150 matches, with an earlier-entry button.
- Reconnect resumes the reducer cursor. Queries refresh on backend invalidation,
  measurements, reconnection, and **Refresh data**. Errors retain old results
  with an error notice.
- Pause/resume acknowledgments display as queued or accepted. Backend events
  and snapshots determine lifecycle status. Failed guidance remains editable.
- Unattached logs differ from empty results. Evaluations are labeled official,
  provisional, or unrecorded. Changed paths are text evidence.
- Refresh preserves the run through the gateway keepalive. The browser owns
  only its requests and subscriptions; it does not start or stop the backend.

## Verification

```sh
pnpm --filter @vibesys/web check
pnpm --filter @vibesys/web test
pnpm --filter @vibesys/web build
pnpm exec playwright install chromium
pnpm test:web-browser
```

Session tests cover replay, acknowledgments, late snapshots, readiness,
backfill, reconnect, query races, errors, cleanup, and escaped rendering.
The Chromium smoke test uses a real backend runtime with deterministic
events and no paid agent calls. See the gateway guide for details.
