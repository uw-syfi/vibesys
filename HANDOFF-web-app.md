# VibeSys web app: handoff

Branch `adi/electron-app-poc`, worktree `~/Documents/vibesys-wt/web-app-shell`. The previous completed app baseline is `cd606b61`; the sandbox startup fix, structured-response parser fix and smoke-test cleanup below are complete.

Spec: `docs/superpowers/specs/2026-09-28-web-app-design.md`. Plans: `docs/superpowers/plans/2026-09-28-{1..6}-*.md`.

Run it with `cd clients && pnpm desktop:start`, or with `pnpm desktop` for dev and HMR.

For an already-open failed/ended run, use `••• → Resume run… → Resume run` to start a fresh gateway with the fixed Python code. Opening the recorded run or relaunching Electron can reuse the old gateway. Starting a new run also creates a fresh gateway.

## Completion update (2026-09-29)

All 6 sub-projects are built and passed their per-task reviews. Plans 1 to 5 also passed their final reviews. The previous whole-branch Electron review and app fixes are complete:

- Missing projects no longer poll runs; availability changes resume polling.
- Occupied ports produce actionable recovery instructions. Authenticated reuse remains intact. A missing discovery file cannot securely recover its random capability, so the shell preserves the listener and explains recovery instead of changing origins automatically.
- Dev mode rejects a reused home whose origin differs from the Vite proxy, preventing capability disclosure to a different local service.
- Verification after `cd606b61`: Python entrypoints/vs-project 717 passed; web/desktop/TUI unit tests 207/22/1144 passed; web e2e 66 passed, 54 skipped. Typechecks, Biome, knip, architecture, format, Python lint and doc links passed.
- Manually verified isolated double-launch reuse/focus and quit cleanup. The macOS sharing indicator covers the traffic-light pixels in native capture, so exact placement remains unverified.

Historical evidence and decisions: `.superpowers/sdd/HANDOFF-web-app/completion.md` (its branch and validation counts predate `cd606b61`).

## Current continuation

- **Codex startup:** Seatbelt's blanket HOME denial blocked explicit provider resources. The fix permits precise declared leaves and ancestor metadata, including declared paths that do not yet exist. Real Codex starts with the profile generated from the current source. The shared structured-response parser now accepts valid JSON arrays for strict tuple fields. Independent review approved the sandbox and parser changes with no blockers.
- **Real app smoke:** a fresh isolated home validated and started the project (HTTP 200), then Codex `gpt-5.6-sol` completed its first implementer call with a valid issue tuple and no execution error or diagnostic. Gateway cleanup (`DELETE live`) returned 200; the harness exited 0 with `ok=true`, `error=null`. This verifies the first full model call, not an entire optimization round. Evidence: `.superpowers/sdd/HANDOFF-web-app/real-codex-smoke-report.json`.
- **Native crash-test cleanup:** Electron's crash dialog blocks graceful quit. The test kills its own Electron child and awaits process close before removing the profile. Native desktop e2e: 2 passed (3.0 s); desktop TypeScript and scoped Biome checks passed. Renderer security assertions remain intact.
- **Current sandbox validation:** 112 passed, 5 Linux-only skips, 1 baseline test deselected. `tests/vibesys/agents/test_host_resource_declarations.py::TestInstallRoot::test_node_package_imports_whole_package_tree` also fails at `cd606b61` on macOS because `/home` canonicalizes to `/System/Volumes/Data/home`. Native Codex with the current source exits 0; static, missing-path and symlink policy cases are covered. Parser regression verified RED/GREEN; 46 narrow tests passed. Repository format (884 files), lint, Tach, test-isolation baseline and diff checks passed.

## Rules for whoever continues

- The owner may have the app open.
  - Tests that start the home server or Electron must use `VIBESYS_HOME_PORT=<not 8764>`, a temporary state home under `$TMPDIR`, and a separate Electron user-data dir.
  - Kill only PIDs you started. Never use `pkill` or kill anything by name.
- Gates, run from `clients/`: `pnpm --filter @vibesys/web test`, tsc, Biome, `pnpm check:knip` (no ignores), the web e2e (`clients/web`: `pnpm exec playwright test e2e/`) and the desktop e2e. For Python, run `PYTHONPATH=src uv run pytest tests/entrypoints libs/vs-project/tests -q` plus the narrow targets for changed modules.
- Never use `git stash`. Commit or push only when the owner asks. PRs need the owner's go-ahead.

## Known follow-ups (not blocking)

- TUI notes writes are non-atomic (`clients/tui/src/notes-store.ts:66`), so the last write wins against the web app.
- The Resume view cannot show a recorded budget for loops without an int budget; `RunRow.budget` is None.
- `KeyVar.label` contract, and making the private `entrypoints.cli` names public (plan 2, M3/M9).
- The Codex critique rulings were run on Claude Opus instead. A Codex pass over the screenshots is optional.

Ledgers, reviews and screenshots: `.superpowers/sdd/<plan>/` (git-excluded) and the session scratchpad `ledgers/`.
