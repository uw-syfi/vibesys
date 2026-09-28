# VibeSys desktop app: design

Status: approved with changes by a Fable review on 2026-09-28 (owner-delegated), after a Codex code review. Branch: `adi/web-ui`.

## Summary

One app that does everything: pick a project, configure a run, start it, watch it, steer it, resume it, and review finished runs. React UI, a long-lived local Python home server, and an Electron shell that launches both. The Python core (runs, agents, journals, run store, run protocol) keeps its contracts; the changes are three bug fixes, a reopen identity path, and small launch options.

The visual design is the approved mockup `2026-09-28-web-app/mockup.html` (Radix Slate neutrals, one Indigo accent, hairline panes, native window chrome). The TUI is the source of concepts and capabilities, not of layout.

## Goals

- Start a run from the app with no CLI steps: folder, task, loop, agents, compute backend, keys, start.
- Watch a live run; reopen and resume finished runs with full data (experiments, performance, design).
- Reach every TUI capability: rounds, agent graph, transcript, pause/resume/steer/stop, experiment chat threads and models, notes, diff, experiments and performance, prompt and todos, themes, command palette.
- One command launches everything in development; Vite hot reload in the browser and the Electron window.

## Non-goals (v1)

- More than one live run per project (one gateway record per project).
- Per-role backends or providers (the run config has one backend/provider; only model and reasoning effort vary per role).
- Per-project provider keys (keys go to the `.env` VibeSys already loads).
- Remote execution environments in setup (Docker, Modal, SkyPilot, Slurm stay CLI flags).
- Minimizing-objective task authoring (new tasks are scalar maximize; existing tasks with other contracts run and display read-only).
- A signed, notarized, Python-bundling distributable (v1 runs from a checkout).
- Live provider-key validation; controls the protocol lacks (skip, approve, manual keep/revert).

## Architecture

```
Electron main ── spawns ──▶ Python home server (127.0.0.1:<stable port>, capability token)
     │ opens window            │ HTTP: app + /api/*
     ▼                         │ spawns, tracks
React app (same code in a browser) ──▶ run server per project (entrypoints.server --web --detach
     │ WebSocket per run            --web-origin <home origin>)
     └───────────────────────────────▶ existing gateway + run protocol
```

- **Home server**: new `vibesys web home` subcommand in `src/entrypoints/web.py`, on stdlib `http.server.ThreadingHTTPServer` (the `websockets` HTTP parser rejects non-GET methods and bodies, so the gateway's HTTP handling cannot be reused; share only the token and Origin helpers). No new Python dependency. It listens on a stable configured port (default 8764, stored in the state home) so run gateways launched with `--web-origin` keep accepting the app across home restarts; run gateways themselves use ephemeral ports. It prints its capability URL on startup.
- **Run servers**: the home server launches `entrypoints.server --web --detach` as `web.py live` does, adding `--web-origin <home origin>` (and the Vite origin in dev), `--web-port 0` (ephemeral; the port is read back from the discovery record, since `web.py:23` hardcodes 8765 and live and reopened gateways must coexist), the existing `BROWSER=true` browser suppression (`web.py:184`), and retained child stderr. It distinguishes three readiness levels: gateway ready (record written), run ready (record attached; see fix 1.2), run failed (child exited, last stderr lines returned).
- **Browser mode**: `vibesys web home --open` serves the same app in a browser.

## Sub-projects

Each has its own implementation plan and stacked PRs. Sub-project 2 lands as three PR groups (fs, validate, catalog, auth; tasks and commit; runs and notes) so 4 can start against the first. Dependencies: 1 and 3 start together; 2 needs nothing but lands before 4, 5 and 6; reopen/resume UI and demo e2e need 1; 4 and 5 need 2 and 3; 6 needs 2's auth contract and a loadable app from 3.

### 1. Backend correctness

1. **Reopen with identity.** Reopen passes only a log directory (`src/entrypoints/server.py:361`, `src/server/runtime.py:160`), and `RunController.attach_read_only` (`src/server/controller.py:60`) attaches only the journal, so `attached_run` is `None` and experiments, performance and design return empty (`src/server/api/service.py:179`, `:399`). Pass project and run identity through the reopen path, resolve the record with `open_run_store(project).get_record(run_id)`, and attach it read-only without lifecycle writes. Test: reopen a finished run fixture; experiments and performance are non-empty and nothing is written to the run.
2. **Readiness signal.** `resources.py:441-444` emits `EXPERIMENTS_CHANGED(reason="project_attached")` before resources publish (`:568`) and before `RunIntegrationAdapter.handle_run_ready` attaches the record (`src/server/integration.py:224-234`). Emit it after the record is attached. Test: a client querying on the signal gets data.
3. **Historical gateways.** `server.py:309` reuses any live project gateway regardless of the requested run. Reopened runs get a run-specific discovery record so a reopen never attaches to the live run's gateway (or vice versa), and records carry optional `run_id` and `mode` (live, reopen) keys under `version: 1` (`_read_record` ignores unknown keys, so `web stop`/`status` keep working).
4. **Demo bundle.** Package the demo as a small recorded project (kept minimal: a run record needs `.vibesys` state on disk) (journal plus run record) reopened through 1.1; the browser-only replay transport (`clients/web/src/replay.ts:33`) returns empty data and is only a fallback.

### 2. Home server and setup API

Auth: HTML and `/api/*` require the home token; run WebSockets keep each gateway's own token and Origin checks (`websocket.py:222-230`); secret-free built assets under the canonical assets root are served without it (the app's `index.html` loads them tokenless). Every state-changing request checks Origin against the exact home origin (and Vite in dev); requests with a null or unexpected Origin are rejected. Keys are never returned or logged. The home server never calls `load_config` or `load_dotenv`; it reads `.env` with `dotenv_values` and keeps its own environment free of provider keys, so a saved key reaches the next launched run instead of being shadowed by a stale value in the home process (`config.py:211`, children inherit `os.environ` at `server.py:254`).

| Endpoint | Purpose | Built on |
|---|---|---|
| `GET /api/fs?path=` | Directories for the folder picker, confined to granted roots (home by default); canonical paths, symlink targets checked against roots, permission errors reported | stdlib; containment like `_layout.py:419` |
| `POST /api/projects/validate` | States: missing, not a git work tree, no commits, dirty tree, uninitialized (no `.vibesys/tasks`), zero tasks, ready (with tasks) | new `validate_project()` over `Project.open`, `is_initialized`, `discover_tasks`, git checks |
| `GET /api/projects` | Recent projects, `$VIBESYS_STATE_HOME/web/recent-projects.json` (default `~/.vibesys`) | new |
| `GET /api/projects/{id}/tasks`, `GET .../tasks/{name}` | Task list and detail (objective, accuracy, benchmark, result contract, domain) and whether it is editable | `select_task`, `load_project_task` |
| `POST .../tasks`, `PUT .../tasks/{name}` | Create or edit a task (writes `OBJECTIVE.md`, `vibesys.input.toml`). Edit only when parse → render → parse round-trips equal (the serializer is known to drop `benchmark.result_protocol`, `_manifest.py:455-485`); otherwise read-only. PUT uses a content hash for conflict detection | `InputManifest` + `render_input_manifest()` |
| `POST .../commit` | Show and commit task-file changes after explicit confirmation (launch rejects dirty or commitless repos, `_git_tracker.py:870`) | git |
| `GET /api/agents/catalog` | Drivers, providers, suggested models, outer loops, compute backends | `agent_catalog()` + the suggested-models list moved from `src/server/chat/options.py` to a shared module; `ComputeBackend` |
| `GET /api/auth` | Per provider: key present (env or `.env`, and whether the process env shadows `.env`), CLI session present (unverified; honors the profile's `state_root_env` and `auth_files`), or missing. CLI-only providers cannot sign in from the app: their row shows the terminal command (e.g. `codex login`) | env, `.env`, `vs_agent` provider profiles |
| `PUT /api/auth/{provider}` | Write-only. Allowlisted variable names per provider, non-empty, no control characters; serialized writes, atomic replace, mode 0600, symlink targets rejected, unrelated entries preserved. Returns status only ("unverified until first run"); reports when a process env var shadows the new value | `.env` at `vibesys.constants.PROJECT_ROOT` (what `load_config` reads, `override=False`) |
| `GET /api/projects/{id}/runs` | Runs newest first with a gateway state: live (verified by attaching and matching run identity), starting, ended-but-serving, stale/unreachable, external (started outside the app, e.g. TUI without `--web`), none | `open_run_store(project).list_runs()`, discovery records |
| `POST /api/projects/{id}/runs` | Start: task, outer loop, loop budget per outer loop as the catalog reports it (`--max-rounds` for agent and plain, `--max-generations` for evolve; profile-guided and dynamic only if a budget flag exists; `cli/constants.py:9`), compute backend, one backend/provider, default model and per-role model/reasoning overrides (written to a run-owned TOML passed with `--config`). Surfaces profile-guided prerequisites. Returns gateway URL and token once the discovery record exists; the client polls `GET .../runs` for starting → live, or failed with the stderr tail | spawns the run server (argv from `args.py:237,451,615`) |
| `POST .../runs/{run}/open` | Reopen read-only (fix 1.1, 1.3) | `--web-reopen` + identity |
| `POST .../runs/{run}/resume` | Resume with the recorded configuration; only non-decreasing total budget | `cli/resume.py:58,228` |
| `DELETE /api/projects/{id}/live` | Stop the live run (SIGTERM) | `web stop` logic |
| `GET/PUT /api/notes/{run}` | Run notes shared with the TUI (`$VIBESYS_STATE_HOME/tui/notes/<run>.json`, same state home and sanitization as `notes-store.ts:31,38`; last write wins) | new |

Errors are typed: `invalid_path`, `outside_roots`, `not_git`, `no_commits`, `dirty_tree`, `uninitialized`, `no_tasks`, `task_invalid` (manifest error), `task_conflict`, `task_read_only`, `already_live`, `launch_failed` (stderr tail), `run_failed`.

### 3. App shell and run view

Replaces the current `clients/web` UI components; keeps `session.ts` transport wiring, `core-state` and `backend-client`.

- **Tokens**: Radix Slate and Indigo with the mockup's contrast table (meaningful text at least 4.5:1 on every surface; field borders and graph edges at least 3:1). System, Light, Dark.
- **Chrome**: sidebar (projects; runs with gateway state; the selected run's rounds as `rN`, two-line titles, delta), title row (run title, status, Pause/Resume with the pending state, labelled retained metric, pane toggle, ••• with Stop and confirm), collapsible sidebar, resizable panes. At 1024 the sidebar collapses before the transcript drops below 560px.
- **Transcript**: sticky round header with a one-line result (attempted vs retained), agent turns with phase names, one-line tool calls (verb plus object, middle-elided commands, tool duration) expanding to output or diff, Prompt and Todos from invocation and todo events, the judge verdict block.
- **Right pane** (scope label "Round N" or "Run"): Ask, Changes (the selected round's available patches with explicit unavailable and truncated states and a reproduction command), Agents, Experiments (plot with legend, per-experiment evidence, design summary), Notes.
- **Agent graph**: `@xyflow/react` (new dependency) read-only, with `@dagrejs/dagre` (already a dependency) top-to-bottom. Nodes are keyed by `execution_id` with `invocation_id` fallback (`src/server/events.py`, `RunEvent` identity fields ~519-540); edges are inferred chronologically within a round and marked as inferred, since events carry no dependency graph; only concurrent executions stack. Clicking a node filters the transcript (fixes the identity collapse in `derive.ts:136`). Codex recommended keeping the existing dagre/SVG renderer instead; React Flow is kept because the owner asked for a library and approved the mockup with it.
- **Composer**: steer only. "Queued" on the pending `CommandAck` (`src/server/api/protocol.py:254`); "Applied" on the consumed steer control event (`controller.py:285`).
- **⌘K palette**: mirrors visible controls; nothing is reachable only through it; ⌘N new run. Every element states one thing; secondary detail (units, timestamps, full commands, key state) is a hover hint.

Replacing the components clears the 22 biome complexity violations noted in merge commit `34f93651`.

### 4. Setup UI

The new-run view on sub-project 2: project states (uninitialized, zero tasks, dirty tree) with next actions; saved task as a summary with Edit (or read-only); new-task form (objective, accuracy command, benchmark command, result JSON argument and metric name, domain); a commit-task-files confirmation; outer loop and budget; compute backend (defaults to the host, e.g. Metal or CPU on a Mac, instead of CUDA); backend/provider and model with per-role model override; provider rows (key present, CLI session unverified, missing, saving, rejected-format, shadowed by env); readiness linking each blocker to its field; Start → starting (gateway ready, baseline) → Round 1; start failure with the stderr tail, Retry and a clickable file location. Resume and reopen entry points from finished runs.

### 5. Ask, notes, palette, themes

Ask tab (thread switcher, new thread, model picker from `query.chat_options`, the no-chat-harness state), notes editor with "Use as steer draft" and "Use as ask draft" (drafts only), theme switcher.

### 6. Electron shell

- `clients/desktop`: main process plus preload, `electron-vite`; added to the architecture checks' package list (`clients/.dependency-cruiser.cjs:3`).
- Main starts `uv run python -m entrypoints.web home` from the checkout, keeps the home capability, and loads the app in a `BrowserWindow` (`titleBarStyle: 'hiddenInset'`). Dev loads the Vite dev server for hot reload.
- Security: `contextIsolation` and `sandbox: true`, no Node integration, a CSP header from the home server, a narrow preload API with sender validation; exact allowed origins per mode (home, Vite in dev); deny external navigation and new windows; never forward the capability off-origin.
- Single instance, a menu; quitting stops the home server, while detached run servers keep running and are rediscovered on next launch.

Distribution (bundled Python, signing, notarization, auto-update) is a later spec.

## Testing

- 1: reopen returns data and writes nothing; readiness signal ordering; reopen never attaches to the live gateway.
- 2: API tests per endpoint and error; key writes never appear in responses or logs, `.env` is 0600, symlinks rejected, other entries preserved; folder browsing confined to roots; Origin and token enforcement.
- 3 to 5: component tests (bun); Playwright e2e against the demo bundle; screenshots at 1440 and 1024 in light and dark, viewed and reviewed by Codex before each PR.
- 6: smoke test that the app starts the home server, loads the UI, and refuses off-origin navigation.
- Existing gates stay green (TUI tests on shared-library changes, `check:ts`, architecture, knip).

## Decisions from open questions

1. Key validation: non-empty, control-character-safe input; shown as "unverified until first run". No live provider calls in v1.
2. Recent projects: `$VIBESYS_STATE_HOME/web/recent-projects.json`, default `~/.vibesys`.
3. PRs: stacked, ultimately targeting main; dependent PRs target their predecessor.

## Appendix: source references

- Reopen, readiness, discovery: `src/server/controller.py:60-78`, `src/server/api/service.py:179-186,399-406`, `src/vibesys/run/resources.py:441-444,568-580`, `src/server/integration.py:224-234`, `src/entrypoints/server.py:253-309,361`, `src/server/runtime.py:160-239`, `src/server/transport/discovery.py:23-131`.
- Launch args and resume: `src/entrypoints/cli/args.py:237,451,615`, `src/entrypoints/cli/resume.py:58,228`.
- Config, keys, providers: `src/vibesys/config.py:84-221`, `libs/vs-agent/src/vs_agent/{catalog,provider_profiles,host_resource_declarations,cli_docker}.py`.
- Manifest: `src/vibesys/inputs/_manifest.py:219-373,455`; evaluation direction `src/vibesys/run/evaluation.py:435`.
