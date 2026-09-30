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

## Run activity continuation (2026-09-29)

- **Apparent stall:** Orion run `20260930-021739-20eae95f-orion-20260930-021739` was active in pre-round planning, then advanced to the implementer. The web app hid its transcript and Agents view while the numeric round was null, showing “Waiting for round 1” despite incoming execution, activity and tool events. Read-only inspection found no terminal run failure; the owner run was left running.
- **UI change:** expose Run activity before a numeric round exists. Preserve explicit history/live selection and earlier-event loading; agent executions retain their real labels and identities.
- **UI validation:** 215 web tests and all 8 actual-home-backed browser checks passed; root inspected the screenshot and independent source review approved the change. Web typecheck, scoped Biome, production build, TypeScript architecture and knip passed. Repository Python format/lint passed (884 files); `uv run pytest tests/server/test_execution_tracker.py tests/server/test_transport_subscription.py --no-cov -q` passed all 42 snapshot/event contract tests. UI source is committed at `a82a2c0c`; earlier sandbox/parser counts above are unchanged.
- **Live build:** authenticated read-only HTTP check confirmed port 8764 serves the current `clients/web/dist/index.html` byte-for-byte. Native CUA could not connect; reload the owner renderer with `Cmd+R` after building.
- **Captured Orion resume failure:** the same run failed at 19:41:28 PDT during `orchestrator-session-turn-2`, after active preparation and implementer work. Codex exited 1 with `thread/resume failed: list_turns is not supported yet (code -32601)`. The original owner run remains failed and was not restarted. Use `Cmd+R` to load the rebuilt UI, then `••• → Resume run… → Resume run` (or start a new run) to create a gateway with the fixed backend.
- **Resume fix:** VibeSys's narrowed state policy omitted SQLite databases required by Codex initialization and resumed history. Exact current database leaves and their WAL, shared-memory and initialization-journal sidecars are now writable; auth/config remain read-only, and provider worktrees remain hidden. Native strict/unconfined probes reproduced the failure and identified each required database. The failed scratch thread recovered its remembered word under precise grants, preserving the original thread ID. A fresh two-turn run under the source-generated profile also retained its thread ID and recalled the word. No conversation-reset fallback or provider argv change was added.
- **Resume validation:** native regressions cover fresh databases, rollback-to-WAL conversion, persisted history, sidecar scope, immutable auth/config and denied worktree reads. The focused sandbox/declaration/parser suite passed 122 tests, with 5 Linux-only skips and the same verified macOS `/home` baseline exclusion documented above. Format, lint, Tach and test-isolation checks passed. Native probe evidence: `/tmp/vibesys-codex-resume/`.
- **Full app resume smoke:** strict AgentShim/Codex `gpt-5.6-sol` completed two implementer turns in run `20260930-030004-aa97d440-candidate-20260930-030004`, with a passing judge between them. Both turns completed with no execution error; the resumed turn recalled the same 12-letter token with zero second-turn tools, proving provider continuity. The harness exited 0 and cleaned up its gateway, temporary state and owned home without errors. Evidence: `.superpowers/sdd/HANDOFF-web-app/real-codex-resume-smoke-report.json` and related logs.

## Rules for whoever continues

- The owner may have the app open.
  - Tests that start the home server or Electron must use `VIBESYS_HOME_PORT=<not 8764>`, a temporary state home under `$TMPDIR`, and a separate Electron user-data dir.
  - Kill only PIDs you started. Never use `pkill` or kill anything by name.
- Gates, run from `clients/`: `pnpm --filter @vibesys/web test`, tsc, Biome, `pnpm check:knip` (no ignores), the web e2e (`clients/web`: `pnpm exec playwright test e2e/`) and the desktop e2e. For Python, run `PYTHONPATH=src uv run pytest tests/entrypoints libs/vs-project/tests -q` plus the narrow targets for changed modules.
- Never use `git stash`. Commit or push only when the owner asks. PRs need the owner's go-ahead.

## Known follow-ups (not blocking)

- Transcript code and tool output have no language syntax highlighting; prompts and output render as plain preformatted text, with diff colors where applicable.
- TUI notes writes are non-atomic (`clients/tui/src/notes-store.ts:66`), so the last write wins against the web app.
- The Resume view cannot show a recorded budget for loops without an int budget; `RunRow.budget` is None.
- `KeyVar.label` contract, and making the private `entrypoints.cli` names public (plan 2, M3/M9).
- The Codex critique rulings were run on Claude Opus instead. A Codex pass over the screenshots is optional.

Ledgers, reviews and screenshots: `.superpowers/sdd/<plan>/` (git-excluded) and the session scratchpad `ledgers/`.
