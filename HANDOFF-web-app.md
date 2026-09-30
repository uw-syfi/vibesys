# VibeSys web app: handoff

Branch `adi/web-app-shell`, worktree `~/Documents/vibesys-wt/web-app-shell`, local only (not pushed). All 6 sub-projects are built and pass their per-task reviews. Plans 1 to 5 also passed their final reviews.

Spec: `docs/superpowers/specs/2026-09-28-web-app-design.md`. Plans: `docs/superpowers/plans/2026-09-28-{1..6}-*.md`.

Run it with `cd clients && pnpm desktop:start`, or with `pnpm desktop` for dev and HMR.

## Completion update (2026-09-29)

The code fixes and final Electron review are complete. Changes remain uncommitted.

- Missing projects no longer poll runs; availability changes resume polling.
- Occupied ports produce actionable recovery instructions. Authenticated reuse remains intact. A missing discovery file cannot securely recover its random capability, so the shell preserves the listener and explains recovery instead of changing origins automatically.
- Dev mode rejects a reused home whose origin differs from the Vite proxy, preventing capability disclosure to a different local service.
- Independent review approved the fixes. Web/desktop unit tests: 207/22 passed. Python: 713 passed. Web e2e: 66 passed, 54 skipped. Desktop e2e: 2 passed. Typechecks, knip, architecture, format, Python lint and doc links passed. Biome retains the two known complexity errors.
- Manually verified isolated double-launch reuse/focus and quit cleanup. The macOS sharing indicator covers the traffic-light pixels in native capture, so exact placement remains unverified.

Detailed evidence and decisions: `.superpowers/sdd/HANDOFF-web-app/completion.md`.

## Original remaining work (completed except noted visual check)

1. **Final whole-branch review of plan 6,** range `e86a6ade..HEAD`. It was stopped before it started. It covers:
   - Electron security: webPreferences, navigation, request and permission guards, the token only in `?token=`, and a preload that exposes `platform` only.
   - Lifecycle: start, reuse, quit, crash, single-instance.
   - Dev versus prod.
   - Accuracy of the docs in `docs/contributing/web-development.md` under "Desktop app".
2. **Port-conflict recovery,** seen by the owner. When a foreign process holds 8764 and `~/.vibesys/web/home.json` is missing, the app shows "cannot listen on 127.0.0.1:8764 (Address already in use)" and stops. The fix goes in `clients/desktop/src/main/home.ts` and `index.ts`:
   - Probe `/health` on the port and reuse the server if it is a VibeSys home.
   - Otherwise explain the problem clearly, or fall back to a free port. The spec keeps the origin stable because run gateways pin the app origin; a new port breaks runs that were already started until they are reopened.
3. **Wasted polling.** The home page polls `GET /api/projects/{id}/runs` every 5 s even when a recent project has `missing: true`, and each call returns 404. Skip runs polling for missing projects: see `clients/web/src/home-hooks.ts` and `home.ts`.
4. **Unverified by hand:**
   - Double-launch single-instance focus. The e2e checks it, but no one has tried it by hand.
   - Native traffic-light placement. Screen capture is black in the agent sandbox.

## Rules for whoever continues

- The owner may have the app open.
  - Tests that start the home server or Electron must use `VIBESYS_HOME_PORT=<not 8764>`, a temporary state home under `$TMPDIR`, and a separate Electron user-data dir.
  - Kill only PIDs you started. Never use `pkill` or kill anything by name.
- Gates, run from `clients/`: `pnpm --filter @vibesys/web test`, tsc, biome (2 known errors: `web/session.ts` and `tui/session-controller.ts` complexity), `pnpm check:knip` (no ignores), the web e2e (`clients/web`: `pnpm exec playwright test e2e/`) and the desktop e2e. For Python, run `PYTHONPATH=src uv run pytest tests/entrypoints libs/vs-project/tests -q`.
- Never use `git stash`. Commit or push only when the owner asks. PRs need the owner's go-ahead.

## Known follow-ups (not blocking)

- TUI notes writes are non-atomic (`clients/tui/src/notes-store.ts:66`), so the last write wins against the web app.
- The Resume view cannot show a recorded budget for loops without an int budget; `RunRow.budget` is None.
- `KeyVar.label` contract, and making the private `entrypoints.cli` names public (plan 2, M3/M9).
- The Codex critique rulings were run on Claude Opus instead. A Codex pass over the screenshots is optional.

Ledgers, reviews and screenshots: `.superpowers/sdd/<plan>/` (git-excluded) and the session scratchpad `ledgers/`.
