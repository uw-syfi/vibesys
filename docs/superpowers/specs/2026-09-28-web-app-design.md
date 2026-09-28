# VibeSys desktop app: design

Status: draft for review, 2026-09-28. Branch: `adi/web-ui`.

## Summary

One app that does everything: pick a project, configure a run, start it, watch it, steer it, and review finished runs. React UI, a long-lived local Python home server, and an Electron shell that launches both. The Python core (runs, agents, journals, run store, run protocol) does not change beyond three bug fixes.

The visual design is the approved mockup `2026-09-28-web-app/mockup.html` (Radix Slate neutrals, one Indigo accent, hairline panes, native window chrome). The TUI is the source of concepts and capabilities, not of layout.

## Goals

- Start a run from the app with no CLI steps: folder, task, agents, keys, start.
- Watch a live run and reopen finished runs with full data (experiments, performance, design).
- Reach every TUI capability: rounds, agent graph, transcript, pause/resume/steer/stop, experiment chat threads and models, notes, diff, experiments and performance, prompt and todos, themes, command palette.
- Launch everything with one command in development (`pnpm app` or equivalent).
- Fast iteration: Vite hot reload for UI work in both the browser and the Electron window.

## Non-goals (v1)

- More than one live run per project at a time (the gateway record is one per project).
- Per-project provider keys (keys go to the `.env` VibeSys already loads).
- Remote execution environments in setup (Docker, Modal, SkyPilot, Slurm stay CLI flags).
- A signed, notarized, Python-bundling distributable. v1 runs the shell from a checkout.
- Controls the protocol does not have (skip round, approve, manual keep/revert).

## Architecture

```
Electron main process ── spawns ──▶ Python home server (127.0.0.1, capability token)
      │ opens window                   │ JSON API + static app
      ▼                                │ spawns, tracks
React app (same code in a browser) ────┼──▶ run server per project
      │ HTTP /api/*                    │    (existing entrypoints.server --web --detach)
      └──── WebSocket per run ─────────┴──▶ existing gateway + run protocol
```

- **Home server**: new `vibesys web home` subcommand in `src/entrypoints/web.py`, built on the same `websockets` HTTP handling, token check and Origin allowlist as `src/server/transport/websocket.py`. No new Python dependency. It serves the built app and `/api/*`, and prints its capability URL on startup (the shell reads it).
- **Run servers**: unchanged. The home server launches them the way `web.py live` does today (`_live_command`) and reads their `WebInstanceRecord` (`<project>/.vibesys/web-gateway.json`). The app connects to a run's gateway directly with that record's URL and token.
- **Browser mode**: `vibesys web home --open` opens the same app in a browser. Useful for development and remote hosts.

## Sub-projects

Each gets its own implementation plan and PR stack on `adi/web-ui`. Order: 1 and 3 can start in parallel; 2 before 4; 6 after 2.

### 1. Backend correctness

1. **Reopen attaches the run record.** `RunController.attach_read_only` (`src/server/controller.py:60`) attaches only the journal, so `attached_run` is `None` and experiments, performance and design queries return empty (`src/server/api/service.py:179`, `:399`). Resolve the record with `open_run_store(project).get_record(run_id)` and attach it as `attach()` does. Test: reopen a finished run fixture and assert non-empty experiments and performance.
2. **Startup race.** `resources.py:441-444` emits `EXPERIMENTS_CHANGED(reason="project_attached")` before resources publish (`:568`) and before `RunIntegrationAdapter.handle_run_ready` attaches the record (`src/server/integration.py:224-234`). Emit the readiness signal after the record is attached. Test: a client that queries on the signal gets data, not an empty response.
3. **Demo bundle.** The demo replays `clients/web/src/fixtures/demo-run.jsonl` with no project, so it cannot answer data queries. Package the demo as a small recorded project (journal plus run record) and reopen it through fix 1.

### 2. Home server and setup API

All endpoints require the token. Requests with an unexpected Origin are rejected. Keys are never returned.

| Endpoint | Purpose | Built on |
|---|---|---|
| `GET /api/fs?path=` | List subdirectories for the folder picker (home directory by default; hidden folders excluded) | stdlib |
| `POST /api/projects/validate` | Directory exists, git work tree, clean tree, `.vibesys/tasks` present, discovered tasks | new `validate_project()` wrapping `Project.open`, `is_initialized()`, `git rev-parse`, `discover_tasks()` (`libs/vs-project/src/vs_project/_layout.py`) |
| `GET /api/projects` | Recent projects (persisted in the VibeSys state home) | new, small JSON file |
| `GET /api/projects/{id}/tasks`, `GET .../tasks/{name}` | Task list and detail (objective, accuracy, benchmark, metric, domain) | `select_task`, `load_project_task` |
| `POST /api/projects/{id}/tasks` | Create a task: writes `OBJECTIVE.md` and `vibesys.input.toml` | `InputManifest` + `render_input_manifest()` (`src/vibesys/inputs/_manifest.py`) |
| `GET /api/agents/catalog` | Drivers, providers, suggested models | `agent_catalog()` (`libs/vs-agent/src/vs_agent/catalog.py`) + the suggested-models list in `src/server/chat/options.py` (moved to a shared module) |
| `GET /api/auth` | Per provider: `key` set / `cli` signed in / `missing` | env and `.env` presence; CLI sign-in inferred from the provider profile's `state_dirs` (`vs_agent/provider_profiles.py`) |
| `PUT /api/auth/{provider}` | Write-only key; validates format, writes the VibeSys `.env` (mode 0600, atomic replace), responds with status only | new; `.env` path from `vibesys.constants.PROJECT_ROOT` (what `load_config` reads) |
| `GET /api/projects/{id}/runs` | Runs newest first, plus the live one | `open_run_store(project).list_runs()`, `WebInstanceRecord.discover` |
| `POST /api/projects/{id}/runs` | Start: task, per-role backend/model, max rounds. Returns the run's gateway URL and token once the record appears | spawns `entrypoints.server --web --detach` with the matching argv |
| `POST /api/projects/{id}/runs/{run}/open` | Reopen a finished run read-only (uses fix 1); returns gateway URL and token | `--web-reopen` path |
| `DELETE /api/projects/{id}/live` | Stop the live run (SIGTERM, as `web stop` does) | `_run_stop` logic |
| `GET/PUT /api/notes/{run}` | Run notes, shared with the TUI (`~/.vibesys/tui/notes/<run>.json`) | same format as `clients/tui/src/notes-store.ts` |

Setup fields map to real inputs: folder, task (existing or new: objective, accuracy command, benchmark command, metric name from `benchmark.result.metric`, domain `generic` or `llm-serving`), agent backend and model with per-role overrides (`agent.toml` `[agent].roles`, passed as run args), max rounds (`--max-rounds`). Metric direction is not a setup field; it comes from the benchmark outcome.

Errors are typed (`invalid_path`, `not_git`, `dirty_tree`, `no_tasks`, `task_invalid` with the manifest error, `key_rejected`, `launch_failed` with the child's last stderr lines, `already_live`).

### 3. App shell and run view

Replaces the current `clients/web` UI components (keeps `session.ts` transport wiring, `core-state`, `backend-client`). New pieces:

- **Tokens**: Radix Slate and Indigo, with the corrected contrast table from the mockup (meaningful text at least 4.5:1 on every surface; field borders and graph edges at least 3:1). System, Light and Dark themes.
- **Chrome**: sidebar (projects, runs, the selected run's rounds with `rN`, two-line titles, delta), a title row (run title, run status, Pause/Resume with the pending state, retained metric with its label, pane toggle, ••• menu with Stop and confirm), a collapsible sidebar and resizable panes.
- **Transcript**: sticky round header with a one-line result (attempted vs retained), agent turns with phase names, tool calls as one line (verb plus object, middle-elided commands, tool duration) that expand to output or diff, Prompt and Todos disclosures from invocation events, and the judge verdict block.
- **Right pane** (scoped label "Round N" or "Run"): Ask, Changes (bound to the selected round, full diff), Agents, Experiments (plot with legend plus per-experiment evidence and design summary), Notes.
- **Agent graph**: `@xyflow/react` (new dependency) read-only, `@dagrejs/dagre` (already a dependency) top-to-bottom. Edges come from execution order and invocation dependencies; only concurrent invocations stack. Each node keeps its `invocationId` so clicking filters the transcript (fixes the current graph's identity collapse).
- **Composer**: steer only, with queued and applied states from `CommandAck`.
- **⌘K palette**: every command routes somewhere; ⌘N new run.
- **1024 wide**: the sidebar collapses before the transcript drops below 560px.

Replacing the components also clears the 22 biome complexity violations recorded in merge commit `34f93651`.

### 4. Setup UI

The new-run view from the mockup on top of sub-project 2: saved task as a summary with Edit, a new-task form, per-role override, provider rows (CLI signed in, key set, missing, checking, rejected), readiness that links each blocker to its field, Start, a starting view (baseline measurement), Round 1, and a start-failure view with Retry and a clickable file location.

### 5. Ask, notes, palette, themes

Experiment chat as the Ask tab (thread switcher, new thread, model picker from `query.chat_options`, the no-chat-harness state), notes editor with "Use as steer draft" and "Use as ask draft" (drafts only), the theme switcher.

### 6. Electron shell

- `clients/desktop` package: Electron main process plus preload, built with `electron-vite`.
- Starts the home server with `uv run python -m entrypoints.web home` from the checkout, reads its printed URL and token, and loads it in a `BrowserWindow` with `titleBarStyle: 'hiddenInset'` (traffic lights in the sidebar as designed). In dev it loads the Vite dev server for hot reload.
- Single instance, a menu, quit kills the home server (run servers are detached and keep running; the app reattaches on next launch).
- Security: `contextIsolation`, no Node integration in the renderer, navigation restricted to the home server origin.
- One command from the repo root starts everything.

Distribution (bundled Python, signing, notarization, auto-update) is a later spec.

## Security

- Home server binds 127.0.0.1 only, requires the capability token on every request (including assets; the gateway's `/assets` exemption is not copied), and checks Origin.
- Keys: write-only API, 0600 file mode, atomic writes, never logged, never echoed; the UI shows status only.
- Folder browsing is read-only and lists directories only.

## Testing

- Sub-project 1: server tests for reopen data and the readiness signal.
- Sub-project 2: API tests per endpoint, including a test that a key PUT never appears in any response or log and that `.env` has mode 0600.
- Sub-projects 3 to 5: component tests (bun), Playwright e2e against the demo bundle, and screenshots at 1440 and 1024 in light and dark, viewed and reviewed by Codex before each PR.
- Sub-project 6: a smoke test that the app starts the home server and loads the UI.
- Existing gates stay green: TUI tests on shared-library changes, `check:ts` (biome), architecture and knip checks.

## Open questions

1. Provider key validation: format check only, or a live provider call (costs a request)? Default: format check plus "checked on first run".
2. Recent projects list location: the VibeSys state home (`~/.vibesys/`) is assumed.
3. The PR target for this branch (main as a stack, or a long-lived integration branch).

## Appendix: source references

- Design brief and reviews: `2026-09-28-web-app/` (mockup, renders).
- Reopen bug and race: `src/server/controller.py:60-78`, `src/server/api/service.py:179-186,399-406`, `src/vibesys/run/resources.py:441-444,568-580`, `src/server/integration.py:224-234`.
- Launch and discovery: `src/entrypoints/server.py:253-287`, `src/entrypoints/web.py:109-197,258-276`, `src/server/transport/discovery.py:23-131`.
- Config and keys: `src/vibesys/config.py:103-221`, `libs/vs-agent/src/vs_agent/{catalog,provider_profiles}.py`.
