# Home Server and Setup API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `vibesys web home`, a long-lived loopback HTTP server that serves the desktop app and a token-protected setup API (folders, projects, tasks, commit, catalog, provider keys, runs, notes).

**Architecture:** A stdlib `ThreadingHTTPServer` in a new `src/entrypoints/web_home/` package, launched from `src/entrypoints/web.py`. One routing table maps each endpoint to a function that takes a `Request` and returns a Pydantic model; `contract.py` is the single definition of every body, response, and error code. Run servers are spawned as `entrypoints.server --web --detach` children whose discovery records live in the state home, never in the candidate repository.

**Tech Stack:** Python 3.12 stdlib (`http.server`, `subprocess`, `hashlib`, `shlex`, `tomllib`), Pydantic v2, python-dotenv (already a dependency: `dotenv_values`, `dotenv.parser.parse_stream`), pytest with Hypothesis.

**Spec:** `docs/superpowers/specs/2026-09-28-web-app-design.md`, section "2. Home server and setup API" (read it with this plan).

## Global Constraints

- No new Python dependency. Stdlib first; `http.server.ThreadingHTTPServer`, not the `websockets` HTTP parser.
- Bind `127.0.0.1` only. Stable port: default `8764`, stored in `$VIBESYS_STATE_HOME/web/home-settings.json` only after a successful bind; run gateways use `--web-port 0` and accept exactly the home origin they were launched with.
- HTML (`/`, `/index.html`) and `/health` require `?token=`; every `/api/*` request requires `Authorization: Bearer <token>`; `/assets/*` under the canonical assets root is served without a token.
- Every `POST`/`PUT`/`DELETE` checks `Origin` against the exact home origin plus `--dev-origin` values; a missing, `null`, or other Origin is rejected. `Host` must be exactly `127.0.0.1:<port>`, the host of the home origin (one allowlist for Host and Origin; `localhost` is not accepted).
- Every response carries the CSP header, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- Keys: allowlisted variable names per provider (profile `auth_env_vars` ending in `_API_KEY` or `_AUTH_TOKEN`), non-empty, no control characters, no `'`, `"`, `\` or `$` (python-dotenv interpolates `${VAR}` even in single quotes); serialized writes, atomic replace, mode `0600`, symlinked `.env` rejected, unrelated entries preserved byte-for-byte (bytes in, bytes out, CRLF kept); values never returned, logged, or echoed in errors.
- Shadowing is membership: a key name present in the home server's inherited environment, even empty, shadows `.env` (`load_dotenv(override=False)` skips it).
- Logs never contain query strings: request logging is method, path, and status only (the raw request line carries the token, possibly percent-encoded).
- Commits only ever include paths under the tasks root (`.vibesys/tasks/`); other `.vibesys/` files (discovery records hold capability tokens) are never committable.
- The home server never calls `load_config` or `load_dotenv`; it reads `.env` with `dotenv_values` and never writes provider keys into `os.environ`.
- Folder browsing and every project path are confined to the granted roots (`--root`, default the home directory) after symlink resolution.
- Recent projects: `$VIBESYS_STATE_HOME/web/recent-projects.json`. Notes: `$VIBESYS_STATE_HOME/tui/notes/<sanitized run>.json`, the TUI's format and sanitization.
- Architecture: `entrypoints` reaches core only through `vibesys.api` / `vibesys.api.request`, libs only through `<lib>.api`; no new `tach.toml` edge is needed (verify with `uv run tach check`).
- Lint suppressions carry a new unique `LW-1013xx` ID and a `# >` rationale (`uv run python scripts/check_lint_waivers.py`). IDs used by this plan: LW-101301 to LW-101308 and LW-101320; confirm each is unused with `grep -rn LW-1013 src libs tests` before adding it.
- Tests: public API only (HTTP against a real server on port 0), no monkeypatch/mock/sleep (`scripts/check_test_isolation.py`), Fakes via injected `HomeConfig` fields.
- Commands in the `adi/web-ui` worktree run with the Bash sandbox off: the worktree is outside the sandbox write allowlist and the tests bind loopback ports.
- Writing style for docs and PR bodies: no em dashes; PRs use `.github/pull_request_template.md`.

## Review Focus

1. Request logging: `BaseHTTPRequestHandler` logs the raw request line, which carries `?token=` for the HTML URL, and `?%74oken=` authenticates after decoding, so a literal-key redaction regex leaks it. Expect method, path, and status only. Test: Task 1 `test_request_logs_never_contain_the_capability_token` (literal and percent-encoded key).
2. A malformed key body (extra field, wrong type): Pydantic's default error text includes `input_value`, which would echo the key. Expect a typed `invalid_request` with locations and messages only. Test: Task 5 parametrized `test_rejected_keys_never_echo_the_value_or_touch_the_file`.
3. A `.env` whose multiline quoted value contains a line that looks like `OPENAI_API_KEY=...`: a line-based rewrite would corrupt it. Expect every other entry preserved byte-for-byte. Tests: Task 5 `test_a_multiline_value_that_mentions_the_key_is_preserved` and the Hypothesis round-trip.
4. DNS rebinding: a page on `evil.test` resolving to 127.0.0.1 reaches the tokenless `/assets/*`. Expect a foreign `Host` header rejected on every path. Test: Task 1 `test_a_foreign_host_header_is_rejected_even_for_tokenless_assets`.
5. Two "Start" clicks racing for one project: expect exactly one run server and one `already_live`. Test: Task 12 `test_start_refuses_a_second_live_run_even_when_racing`.

## Dependency on sub-project 1

Group (c) (Tasks 12 to 16) assumes sub-project 1 (`docs/superpowers/plans/2026-09-28-1-backend-correctness.md`, Tasks 1 and 3) has landed. Names used here, as that plan defines them:

- `WebInstanceRecord` gains `run_id: str | None = None` and `mode: Literal["live", "reopen"] = "live"`; `version` stays `1`. Only reopen records carry `run_id`; a live record's run is not in it.
- Reopen: `entrypoints.server --web --detach --project <root> --web-reopen-run <run_id>`. Without `--web-reopen`, the journal comes from `project.state.log_directory(run_id)` and the record from the run store. The home server never passes `--web-reopen` (journal-only reopens exist for the demo bundle).
- `--project` becomes the gateway's recorded `project_root` (`ServerRuntime(project_root=...)`). The home server always passes `--project`.
- Plan 1's default record paths are `<project>/.vibesys/web-gateway.json` (live), `web-gateway-<run_id>.json` (`--web-reopen-run`), and `web-gateway-log-<sha256(canonical log dir)[:12]>.json` (journal-only). The home server does not use these defaults: it always passes `--web-instance` under `$VIBESYS_STATE_HOME/web/gateways/<project id>/` (`live.json`, `reopen-<run>.json`), so its records never dirty the candidate tree. It reads plan 1's `.vibesys/web-gateway.json` to report `external` and refuse a second live run, and `.vibesys/web-gateway-<run_id>.json` to reuse a reopen another launcher started. The home server marks spawned children with `VIBESYS_DETACHED_CHILD=1`, so the launcher's reuse check (refuse a record whose `(mode, run_id)` differs) never runs for them; distinct instance paths per mode and run keep the two plans consistent.
- Because live records carry no `run_id`, the home server writes an owner sidecar `live.owner.json` (`run_id`, `pid`, `origin`) atomically per launch attempt, trusts a record only when its pid matches, and attributes an external live record to the project's current run.

Groups (a) and (b) have no dependency and can merge first.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/entrypoints/web_home/__init__.py` | Package docstring |
| `src/entrypoints/web_home/contract.py` | Every request/response model, `ErrorCode`, `ApiError`, JSON Schema printer |
| `src/entrypoints/web_home/context.py` | `HomeConfig` (injected host state), `Request`, `parse_body`, `atomic_write`, `git`, `pending_changes`, `safe_segment` |
| `src/entrypoints/web_home/app.py` | `HomeServer`, handler (Host, token, Origin, CSP, assets, log redaction), route table, `run_home` |
| `src/entrypoints/web_home/projects.py` | `/api/fs`, validate, recent projects, `resolve_project` |
| `src/entrypoints/web_home/catalog.py` | `/api/agents/catalog`, the loop and budget table |
| `src/entrypoints/web_home/keys.py` | `/api/auth`, `.env` writes |
| `src/entrypoints/web_home/tasks.py` | Task list/detail/create/edit, commit preview and commit |
| `src/entrypoints/web_home/runs.py` | Run list with gateway states, start, open, resume, stop |
| `src/entrypoints/web_home/notes.py` | `/api/notes/{run}` |
| `src/entrypoints/web.py` | `home` subcommand |
| `libs/vs-project/src/vs_project/{_state,_layout,project}.py`, `api/__init__.py` | `state_home()`, `Project.create_task`, `TaskExistsError` |
| `libs/vs-agent/src/vs_agent/{provider_policy,provider_profiles,host_resource_declarations}.py`, `api/__init__.py` | `SUGGESTED_MODELS`, `state_dir_path`, `credential_path`, export `provider_profile` |
| `src/vibesys/config.py`, `src/vibesys/api/__init__.py`, `src/vibesys/api/request.py` | `DOTENV_PATH`, re-exports, `orchestration_roles` |
| `src/server/chat/options.py` | Reads `SUGGESTED_MODELS` from `vibesys.api` |
| `tests/entrypoints/web_home/` | `conftest.py`, `support.py`, `fake_run_server.py`, `test_home_*.py` |
| `libs/vs-project/tests/test_create_task.py` | `Project.create_task` |

## API contract

Sub-projects 4, 5 and 6 plan against this section. `contract.py` is the source of truth; `uv run python -m entrypoints.web_home.contract` prints its JSON Schema.

### Transport

- Base URL: `http://127.0.0.1:<port>` (default 8764). The app is opened at `/?token=<token>`; `vibesys web home` prints `VibeSys home: <that URL>` as its only stdout line.
- API auth: header `Authorization: Bearer <token>` on every `/api/*` call.
- `POST`/`PUT`/`DELETE` also need `Origin` equal to the home origin or a `--dev-origin`. In development the Vite server must proxy `/api` to the home server with `changeOrigin: true` (so `Host` is rewritten) and the browser's Vite `Origin` passed through; start home with `--dev-origin http://127.0.0.1:5173`. `clients/web/vite.config.ts` has no proxy today; plan 3 (app shell) owns adding it, and plan 6 (Electron dev mode) relies on it.
- Port changes: run gateways accept only the home origin they were launched with. After `--port` changes the origin, `vibesys web home` logs each serving home-launched gateway that will reject the app; the API reports them with `gateway.origin_mismatch: true`. Reopen gateways are restarted by `POST .../open`; for a live run the UI offers stop, then resume.
- Bodies are JSON (`Content-Type: application/json`, at most 1 MiB). Unknown body keys are rejected.
- Success is always `200` with the documented body. Path segments are percent-decoded.
- Errors: `{"error": {"code": ErrorCode, "message": str, "details": object | null}}`.

| `code` | HTTP | Raised by | `details` |
| --- | --- | --- | --- |
| `unauthorized` | 401 | missing or wrong token | |
| `forbidden_origin` | 403 | bad `Origin` on a write, or bad `Host` | |
| `not_found` | 404 | unknown route or asset | |
| `invalid_request` | 400 | body fails validation, unknown outer loop, bad run config | `{"errors": [{"loc": [str], "msg": str}]}` |
| `internal_error` | 500 | unexpected exception | |
| `invalid_path` | 400 | relative/NUL path, not a directory; project `missing` at launch | |
| `outside_roots` | 403 | path resolves outside the granted roots | |
| `permission_denied` | 403 | folder unreadable | |
| `unknown_project` | 404 | project id not in recent projects | |
| `not_git`, `no_commits`, `dirty_tree`, `uninitialized`, `no_tasks` | 409 | start preflight | `{"pending": [str]}` |
| `unknown_provider` | 404 | provider not shipped, or not run by the chosen driver | |
| `invalid_key` | 400 | variable not allowlisted; empty value; control character, quote, backslash, or `$` | |
| `symlink_rejected` | 409 | `.env` (or any file the server replaces) is a symlink | |
| `unknown_task` | 404 | task name not found | |
| `task_invalid` | 422 | layout or manifest error, bad task name, unparseable command | `{"errors": [...]}` when from validation |
| `task_exists` | 409 | create with a taken name | |
| `task_conflict` | 409 | stale `base_hash`; commit paths differ from the preview | commit: `{"task_files": [str]}` |
| `task_read_only` | 409 | edit of a task the form cannot round-trip | |
| `commit_failed` | 422 | `git add`/`git commit` failed | `{"stderr_tail": [str]}` |
| `unknown_run` | 404 | run id not in the project's run store | |
| `already_live` | 409 | the project already has a live gateway (home-launched or external) | `{"run_id": str \| null}` |
| `launch_failed` | 502 | run server exited, or published no record within 30 s (then it is terminated and reaped) | `{"stderr_tail": [str] /* last 40 lines */, "stderr_log": str /* absolute path of the retained stderr file */}` |
| `profile_guided_unavailable` | 422 | profile-guided loop on a task without `[profile_guided]` | |
| `budget_decrease` | 422 | resume budget below the recorded total | `{"recorded": int}` |
| `not_resumable` | 422 | orchestration the CLI cannot resume | |

### Endpoints

Types below use TypeScript notation; `?` marks nullable fields that are always present.

**`GET /api/fs?path=<abs>`** (no `path`: list the roots)
```ts
type FsListing = { path: string | null; parent: string | null; entries: FsEntry[] };
type FsEntry = { name: string; path: string /* canonical */; git: boolean };
```
Entries are non-hidden subdirectories, sorted case-insensitively; a symlink appears under its own name with its canonical target `path`, and only when the target is inside a root. `parent` is null at a root. Errors: `invalid_path`, `outside_roots`, `permission_denied`.

**`POST /api/projects/validate`** body `{ path: string }`
```ts
type ProjectState = "missing" | "not_git" | "invalid" | "uninitialized" | "no_tasks" | "no_commits" | "dirty_tree" | "ready";
type ProjectRef = { id: string /* 16 hex */; root: string; name: string };
type ProjectValidation = { state: ProjectState; path: string; project: ProjectRef | null; message: string | null; tasks: string[]; pending: string[] };
```
`state` is the first blocker in the listed order. `project` is set once the folder is a git work tree, and such folders are added to recent projects. `message` explains `invalid`. `pending` lists changed and untracked paths relative to the project (only when it has commits). Errors: `invalid_path`, `outside_roots`.

**`GET /api/projects`**
```ts
type ProjectList = { projects: (ProjectRef & { last_opened: string /* ISO 8601 */ })[] };  // most recent first, at most 20
```

**`GET /api/projects/{id}/tasks`**
```ts
type TaskList = { tasks: { name: string; valid: boolean; domain: string | null; error: string | null }[] };
```
Errors: `unknown_project`, `task_invalid` (the tasks root itself is unusable).

**`GET /api/projects/{id}/tasks/{name}`**, and the response of create and edit
```ts
type TaskDetail = {
  name: string; objective: string; domain: "llm-serving" | "generic" | "microservices" | "database";
  accuracy_command: string; benchmark_command: string;            // shell-quoted, resolved argv
  result: { kind: "metric" | "protocol" | "none"; json_argument: string | null; metric: string | null; protocol_version: number | null };
  profile_guided: boolean;                                         // task has [profile_guided]
  editable: boolean; read_only_reason: string | null;
  content_hash: string;                                            // sha256 of OBJECTIVE.md, NUL, vibesys.input.toml
};
```
A task is editable only when both commands are `command` argv (not evaluator entrypoints), it declares `[benchmark.result]`, and parse, render, parse of its manifest is equal. Errors: `unknown_project`, `unknown_task`, `task_invalid`.

**`POST /api/projects/{id}/tasks`** body
```ts
type TaskForm = { objective: string /* non-empty */; domain: TaskDetail["domain"]; accuracy_command: string; benchmark_command: string; result_json_argument: string /* e.g. "--json" */; result_metric: string };
type TaskCreate = TaskForm & { name: string /* [a-z0-9][a-z0-9._-]{0,127} */ };
```
Writes `.vibesys/tasks/<name>/OBJECTIVE.md` and `vibesys.input.toml` (scalar maximize: `[benchmark.result]`). Commands are split with `shlex`. Returns `TaskDetail`. Errors: `task_exists`, `task_invalid`, `invalid_request`.

**`PUT /api/projects/{id}/tasks/{name}`** body `TaskForm & { base_hash: string }`. Applies the form onto the existing manifest (settings the form does not show, such as timeouts, are kept). Returns `TaskDetail`. Errors: `task_conflict`, `task_read_only`, `task_invalid`, `unknown_task`.

**`GET /api/projects/{id}/commit`**
```ts
type CommitPreview = { task_files: string[] /* under .vibesys/tasks/ only */; other: string[] };
```

**`POST /api/projects/{id}/commit`** body `{ paths: string[] /* exactly preview.task_files */; message: string | null }`
```ts
type CommitResult = { commit: string /* HEAD sha */; committed: string[] };
```
Commits only files under the tasks root (`.vibesys/tasks/`), also in a repository with no commits; any other `.vibesys/` change (for example a `web-gateway*.json` with its token) is listed in `other` and never committed. `other` changes stay; the project then validates as `dirty_tree` until the user handles them. Errors: `task_conflict`, `commit_failed`.

**`GET /api/agents/catalog`**
```ts
type Catalog = {
  drivers: { driver: "agentshim" | "omnigent"; providers: string[]; supports_docker: boolean }[];
  providers: { provider: string; display_name: string; supports_reasoning_effort: boolean; suggested_models: string[] }[];
  outer_loops: { id: "agent" | "profile-guided" | "dynamic" | "plain" | "evolve"; budget: { flag: "--max-rounds" | "--max-generations"; default: number }; requires_profile_guided: boolean; roles: string[] }[];
  compute_backends: ("cuda" | "metal" | "trainium" | "rocm" | "cpu")[];
  default_compute_backend: Catalog["compute_backends"][number];  // metal on Apple silicon, else cuda/rocm if their CLI exists, else cpu
};
```

**`GET /api/auth`**
```ts
type AuthStatus = { dotenv_path: string; providers: ProviderAuth[] };
type ProviderAuth = {
  provider: string; display_name: string;
  status: "key" | "cli_session" | "missing";
  keys: { name: string; source: "env" | "dotenv" | "missing"; shadowed: boolean /* name is in the inherited environment (even empty) and in .env */ }[];
  cli_session: "present" | "absent" | "unknown";  // presence only, never verified: credential file, or for Claude on macOS the keychain item "Claude Code-credentials"
  login_command: string;         // e.g. "codex login", "opencode auth login", else the CLI binary
};
```
`keys` is empty for CLI-only providers (opencode). `status` is `key` if any key is set, else `cli_session` when `cli_session` is `present`, else `missing`; the UI labels a present session "present (unverified)". `unknown` means the keychain could not be asked (timeout, no `security` tool).

**`PUT /api/auth/{provider}`** body `{ name: string; value: string }`
```ts
type KeyWriteResult = { provider: string; name: string; status: "unverified"; shadowed_by_env: boolean /* name is in the inherited environment */ };
```
Errors: `unknown_provider`, `invalid_key`, `symlink_rejected`, `invalid_request`.

**`GET /api/projects/{id}/runs`**
```ts
type GatewayState = "live" | "starting" | "ended_serving" | "failed" | "stale" | "external" | "reopened" | "none";
type Gateway = {
  state: GatewayState; url: string | null; websocket_url: string | null; token: string | null;
  stderr_tail: string[]; stderr_log: string | null;  // set for "failed": clickable diagnostic file
  origin_mismatch: boolean;                          // gateway was launched for another home origin
};
type RunRow = { run_id: string; loop: string | null; status: "unknown" | "active" | "completed" | "failed"; rounds: number; gateway: Gateway; reopen: Gateway | null; error: string | null;
  task: string | null; objective: string | null /* the record's effective objective */; created_at: string | null /* ISO 8601, from the manifest */ };  // the three are null for launches not yet in the run store
type RunList = { runs: RunRow[] };  // launches not yet in the run store first, then the store newest first
```
Gateway states come from the gateway record plus the latest attempt in the run's `run-events.jsonl` (an attempt begins at the last `server_started`; a resume appends a new one, so an earlier attempt's terminal event never counts):
- `starting`: this server spawned it and no record yet, or the record answers but the attempt has no `experiments_changed` with `reason: "project_attached"` (sub-project 1 emits it after the run record attaches; the manifest alone does not mean ready).
- `live`: the record answers `/health` with mode `live`, its owner sidecar names this run and the record's pid, and the attempt is attached with no `run_finished`.
- `ended_serving`: the attempt has a terminal event (`run_finished`, `run_failed`, or `run_interrupted`); the gateway still serves.
- `failed`: this server's launch exited with a positive status; `stderr_tail` and `stderr_log` set.
- `stale`: record file present, gateway does not answer.
- `external`: `.vibesys/web-gateway.json` from the TUI or `web live`, attributed to the project's current run (live records carry no run id).
- `none`.

`url`, `websocket_url`, `token` are set only for gateways that answer. `reopen` is the run's read-only gateway when one serves (ours, or plan 1's `.vibesys/web-gateway-<run>.json`, matched on `mode == "reopen"` and `run_id`). `status` is derived, never copied from the store (which always reports `unknown`): `failed` for a failed launch, `active` while a gateway is `live`, `starting` or `external`, else the latest attempt's terminal event (`run_finished` gives `completed`; `run_failed` and `run_interrupted` give `failed`), else `unknown`.

**`POST /api/projects/{id}/runs`** body
```ts
type StartRun = {
  task: string; outer_loop: Catalog["outer_loops"][number]["id"]; budget: number | null /* >= 1, the loop's budget flag */;
  compute_backend: Catalog["compute_backends"][number];
  driver: "agentshim" | "omnigent" | null; provider: string; model: string;
  reasoning_effort: string | null;
  roles: Record<string /* role id from the catalog */, { model: string | null; reasoning_effort: string | null }>;
};
type LaunchResult = { run_id: string; gateway: Gateway /* state "starting", url/token set */ };
```
Returns once the run server's discovery record exists (gateway ready); poll `GET .../runs` for `starting` to `live`, or `failed`. Errors: `already_live`, the five preflight codes, `unknown_task`, `unknown_provider`, `profile_guided_unavailable`, `invalid_request`, `launch_failed`.

**`POST /api/projects/{id}/runs/{run}/resume`** body `{ budget: number | null }` returns `LaunchResult`. The CLI restores the recorded configuration (`--resume` with `--outer-loop`); only the budget is passed. Errors: `unknown_run`, `not_resumable`, `budget_decrease`, `already_live`, `launch_failed`.

**`POST /api/projects/{id}/runs/{run}/open`** body `{}` returns `LaunchResult` with state `reopened`; reuses a serving reopen gateway (ours or another launcher's) or spawns `--project <root> --web-reopen-run <run>`. Errors: `unknown_run`, `launch_failed`.

**`DELETE /api/projects/{id}/live`** returns `{ stopped: boolean; run_id: string | null }` after sending SIGTERM (home-launched gateway first, else the external one).

**`GET /api/notes/{run}`**, **`PUT /api/notes/{run}`** body `{ text: string }`
```ts
type NoteResponse = { note: { runId: string; text: string; createdAt: string; updatedAt: string } | null };  // TUI NoteRecord, camelCase
```
Last write wins; `createdAt` is kept across writes. Errors: `invalid_request` (empty run id, or over 200 characters once sanitized, a non-BMP character counting twice).

---

# PR group (a): server, folders, projects, catalog, auth

### Task 1: `vibesys web home` server with auth, assets, and the contract

**Files:**
- Create: `src/entrypoints/web_home/__init__.py`, `contract.py`, `context.py`, `app.py`
- Modify: `src/entrypoints/web.py` (parser and `main`), `libs/vs-project/src/vs_project/_state.py`, `libs/vs-project/src/vs_project/api/__init__.py`, `src/vibesys/config.py:27,208`, `src/vibesys/api/__init__.py`
- Test: `tests/entrypoints/web_home/conftest.py`, `support.py`, `test_home_server.py`

**Interfaces:**
- Produces: `HomeConfig(state_home, roots, dotenv_path, assets_dir, port, dev_origins=(), environ=os.environ, clock=_utc_now, token=..., write_lock=Lock())`, `HomeConfig.origin -> str`; `Request(config, params: tuple[str, ...], query, body: bytes)`, `Request.arg(name) -> str | None`; `parse_body(request, Model) -> Model`; `validation_errors(ValidationError) -> list[JsonValue]`; `atomic_write(path, data: bytes, *, mode: int)`; `ApiError(code, message, *, details=None)`, `ErrorCode`; `HomeServer(config)` (`.url`); `saved_port(web_dir) -> int`, `save_port(web_dir, port)` (called only after a successful bind); `run_home(args, repository_root) -> int`; `DEFAULT_PORT = 8764`; `vs_project.api.state_home() -> Path`; `vibesys.api.DOTENV_PATH`.
- Route handlers everywhere have the signature `(request: Request) -> BaseModel` and are registered in `app._ROUTES` as `(METHOD, re.compile(path), handler)`.

- [ ] **Step 1: Expose the state home and the `.env` path**

In `libs/vs-project/src/vs_project/_state.py`, add above `def _state_home()`:

```python
def state_home() -> Path:
    """Return the machine-local VibeSys state root.

    ``$VIBESYS_STATE_HOME`` when set (it must be absolute), else ``~/.vibesys``.
    """
    return _state_home()
```

Export it: in `libs/vs-project/src/vs_project/api/__init__.py` add `state_home,` to the `from vs_project._state import (...)` list after `is_project_state_path,`, and `"state_home",` to `__all__` after `"is_project_state_path",`.

In `src/vibesys/config.py`, after `BUNDLED_RESOURCES = ...` add:

```python
DOTENV_PATH = PROJECT_ROOT / ".env"
"""The `.env` that `load_config` reads (existing environment variables win)."""
```

and change `def _load_dotenv_file(path: Path = PROJECT_ROOT / ".env") -> None:` to `def _load_dotenv_file(path: Path = DOTENV_PATH) -> None:`. In `src/vibesys/api/__init__.py` add `from vibesys.config import DOTENV_PATH` after the `vibesys.composition` import and `"DOTENV_PATH",` as the first `__all__` entry.

- [ ] **Step 2: Write the test support and the failing server tests**

`tests/entrypoints/web_home/support.py`:

```python
"""Client helpers shared by the home server tests."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from entrypoints.web_home.context import HomeConfig


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


@dataclass
class Home:
    config: HomeConfig
    workspace: Path
    default_headers: dict[str, str] = field(default_factory=dict)

    def send(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Reply:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310
            self.config.origin + path,
            data=data,
            method=method,
            headers=dict(self.default_headers if headers is None else headers),
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return Reply(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as error:
            return Reply(error.code, error.read(), dict(error.headers))

    def get(self, path: str) -> Reply:
        return self.send("GET", path)

    def post(self, path: str, body: object = None) -> Reply:
        return self.send("POST", path, {} if body is None else body)

    def put(self, path: str, body: object) -> Reply:
        return self.send("PUT", path, body)

    def delete(self, path: str) -> Reply:
        return self.send("DELETE", path)
```

`tests/entrypoints/web_home/conftest.py`:

```python
"""A real home server on an ephemeral loopback port, with injected host state."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from entrypoints.web_home.app import HomeServer
from entrypoints.web_home.context import HomeConfig
from tests.entrypoints.web_home.support import Home

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def home(tmp_path: Path) -> Iterator[Home]:
    assets = tmp_path / "dist"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("<!doctype html><title>VibeSys</title>\n")
    (assets / "assets" / "app.js").write_text("console.log('app');\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    config = HomeConfig(
        state_home=tmp_path / "state",
        roots=(workspace.resolve(),),
        dotenv_path=tmp_path / "checkout" / ".env",
        assets_dir=assets.resolve(),
        port=0,
        dev_origins=("http://127.0.0.1:5173",),
        # The spawned run server must resolve runs in the same isolated state home.
        environ={
            "HOME": str(user_home),
            "PATH": "/usr/bin:/bin",
            "VIBESYS_STATE_HOME": os.environ["VIBESYS_STATE_HOME"],
        },
        clock=lambda: datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
    )
    server = HomeServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Home(
            config=config,
            workspace=workspace.resolve(),
            default_headers={
                "Authorization": f"Bearer {config.token}",
                "Origin": config.origin,
                "Content-Type": "application/json",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
```

`tests/entrypoints/web_home/test_home_server.py`:

```python
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from entrypoints.web import _parser
from entrypoints.web_home.app import DEFAULT_PORT, save_port, saved_port
from server.runtime import WebInstanceRecord

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.entrypoints.web_home.support import Home


def test_index_needs_the_capability_token_and_carries_security_headers(home: Home) -> None:
    assert home.send("GET", "/", headers={}).json()["error"]["code"] == "unauthorized"

    reply = home.send("GET", f"/?token={home.config.token}", headers={})

    assert reply.status == 200
    assert b"<title>VibeSys</title>" in reply.body
    assert "default-src 'self'" in reply.headers["Content-Security-Policy"]
    assert "connect-src 'self' ws://127.0.0.1:*" in reply.headers["Content-Security-Policy"]
    assert reply.headers["Referrer-Policy"] == "no-referrer"
    assert reply.headers["Cache-Control"] == "no-store"


def test_built_assets_are_served_without_a_token_but_only_below_assets(home: Home) -> None:
    assert home.send("GET", "/assets/app.js", headers={}).status == 200
    assert home.send("GET", "/assets/%2e%2e/index.html", headers={}).status == 404
    assert home.send("GET", "/assets/missing.js", headers={}).status == 404


def test_api_requires_the_bearer_token(home: Home) -> None:
    assert home.send("GET", "/api/nope", headers={}).status == 401
    wrong = {"Authorization": "Bearer wrong"}
    assert home.send("GET", "/api/nope", headers=wrong).status == 401
    assert home.get("/api/nope").json()["error"]["code"] == "not_found"


def test_state_changing_requests_need_an_exact_allowed_origin(home: Home) -> None:
    token = {"Authorization": f"Bearer {home.config.token}"}
    for origin in (None, "null", "http://evil.test", "http://localhost:5173"):
        headers = token if origin is None else {**token, "Origin": origin}
        reply = home.send("POST", "/api/nope", {}, headers=headers)
        assert reply.json()["error"]["code"] == "forbidden_origin", origin
    for origin in (home.config.origin, "http://127.0.0.1:5173"):
        reply = home.send("POST", "/api/nope", {}, headers={**token, "Origin": origin})
        assert reply.json()["error"]["code"] == "not_found", origin


def test_a_foreign_host_header_is_rejected_even_for_tokenless_assets(home: Home) -> None:
    for host in (f"evil.test:{home.config.port}", f"localhost:{home.config.port}"):
        reply = home.send("GET", "/assets/app.js", headers={"Host": host})
        assert reply.json()["error"]["code"] == "forbidden_origin", host


def test_request_logs_never_contain_the_capability_token(
    home: Home, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="entrypoints.web_home.app"):
        home.send("GET", f"/?token={home.config.token}", headers={})
        home.send("GET", "/?token=guess", headers={})
        encoded = home.send("GET", f"/?%74oken={home.config.token}", headers={})

    assert encoded.status == 200
    assert "GET / 200" in caplog.text
    assert home.config.token not in caplog.text
    assert "guess" not in caplog.text
    assert "oken" not in caplog.text


def test_health_answers_the_discovery_probe(home: Home, tmp_path: Path) -> None:
    record = WebInstanceRecord.from_gateway(
        pid=os.getpid(), port=home.config.port, token=home.config.token, project_root=tmp_path
    )
    path = tmp_path / "home.json"
    record.write(path)

    assert WebInstanceRecord.discover(path) == record


def test_the_saved_port_defaults_and_round_trips(tmp_path: Path) -> None:
    assert saved_port(tmp_path) == DEFAULT_PORT
    save_port(tmp_path, 9100)
    assert saved_port(tmp_path) == 9100
    (tmp_path / "home-settings.json").write_text('{"port": "nope"}')
    assert saved_port(tmp_path) == DEFAULT_PORT


def test_web_home_parses_its_options() -> None:
    args = _parser().parse_args(
        ["home", "--port", "9100", "--root", "/srv", "--dev-origin", "http://127.0.0.1:5173"]
    )

    assert (args.command, args.port, [str(r) for r in args.root]) == ("home", 9100, ["/srv"])
    assert args.dev_origin == ["http://127.0.0.1:5173"]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/entrypoints/web_home/test_home_server.py -q --basetemp=/tmp/vsw`
Expected: collection error, `ModuleNotFoundError: No module named 'entrypoints.web_home'`.

- [ ] **Step 4: Write the contract**

`src/entrypoints/web_home/__init__.py`:

```python
"""The long-lived local home server behind the VibeSys desktop app (`vibesys web home`)."""
```

`src/entrypoints/web_home/contract.py` (group (a) models; groups (b) and (c) append theirs):

```python
"""JSON contract of the home server API: request bodies, responses, and errors.

This module is the one authoritative definition of the contract. Clients
generate their types from the JSON Schema printed by
``python -m entrypoints.web_home.contract``.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from http import HTTPStatus
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

from vibesys.api import ComputeBackend
from vs_agent.api import Driver


class ErrorCode(StrEnum):
    """Every typed error the API returns, as ``error.code``."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN_ORIGIN = "forbidden_origin"
    NOT_FOUND = "not_found"
    INVALID_REQUEST = "invalid_request"
    INTERNAL = "internal_error"
    INVALID_PATH = "invalid_path"
    OUTSIDE_ROOTS = "outside_roots"
    PERMISSION_DENIED = "permission_denied"
    UNKNOWN_PROJECT = "unknown_project"
    NOT_GIT = "not_git"
    NO_COMMITS = "no_commits"
    DIRTY_TREE = "dirty_tree"
    UNINITIALIZED = "uninitialized"
    NO_TASKS = "no_tasks"
    UNKNOWN_PROVIDER = "unknown_provider"
    INVALID_KEY = "invalid_key"
    SYMLINK_REJECTED = "symlink_rejected"
    UNKNOWN_TASK = "unknown_task"
    TASK_INVALID = "task_invalid"
    TASK_EXISTS = "task_exists"
    TASK_CONFLICT = "task_conflict"
    TASK_READ_ONLY = "task_read_only"
    COMMIT_FAILED = "commit_failed"
    UNKNOWN_RUN = "unknown_run"
    ALREADY_LIVE = "already_live"
    LAUNCH_FAILED = "launch_failed"
    PROFILE_GUIDED_UNAVAILABLE = "profile_guided_unavailable"
    BUDGET_DECREASE = "budget_decrease"
    NOT_RESUMABLE = "not_resumable"


_STATUS: dict[ErrorCode, HTTPStatus] = {
    ErrorCode.UNAUTHORIZED: HTTPStatus.UNAUTHORIZED,
    ErrorCode.FORBIDDEN_ORIGIN: HTTPStatus.FORBIDDEN,
    ErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.INVALID_REQUEST: HTTPStatus.BAD_REQUEST,
    ErrorCode.INTERNAL: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCode.INVALID_PATH: HTTPStatus.BAD_REQUEST,
    ErrorCode.OUTSIDE_ROOTS: HTTPStatus.FORBIDDEN,
    ErrorCode.PERMISSION_DENIED: HTTPStatus.FORBIDDEN,
    ErrorCode.UNKNOWN_PROJECT: HTTPStatus.NOT_FOUND,
    ErrorCode.NOT_GIT: HTTPStatus.CONFLICT,
    ErrorCode.NO_COMMITS: HTTPStatus.CONFLICT,
    ErrorCode.DIRTY_TREE: HTTPStatus.CONFLICT,
    ErrorCode.UNINITIALIZED: HTTPStatus.CONFLICT,
    ErrorCode.NO_TASKS: HTTPStatus.CONFLICT,
    ErrorCode.UNKNOWN_PROVIDER: HTTPStatus.NOT_FOUND,
    ErrorCode.INVALID_KEY: HTTPStatus.BAD_REQUEST,
    ErrorCode.SYMLINK_REJECTED: HTTPStatus.CONFLICT,
    ErrorCode.UNKNOWN_TASK: HTTPStatus.NOT_FOUND,
    ErrorCode.TASK_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.TASK_EXISTS: HTTPStatus.CONFLICT,
    ErrorCode.TASK_CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.TASK_READ_ONLY: HTTPStatus.CONFLICT,
    ErrorCode.COMMIT_FAILED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.UNKNOWN_RUN: HTTPStatus.NOT_FOUND,
    ErrorCode.ALREADY_LIVE: HTTPStatus.CONFLICT,
    ErrorCode.LAUNCH_FAILED: HTTPStatus.BAD_GATEWAY,
    ErrorCode.PROFILE_GUIDED_UNAVAILABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.BUDGET_DECREASE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.NOT_RESUMABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class ApiError(Exception):
    """A typed API failure; the server renders it as an ``ErrorBody``."""

    def __init__(
        self, code: ErrorCode, message: str, *, details: dict[str, JsonValue] | None = None
    ) -> None:
        """Carry the code, a user-facing message, and optional structured details."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def status(self) -> HTTPStatus:
        """Return the HTTP status this error is sent with."""
        return _STATUS[self.code]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorDetail(_Model):
    """The ``error`` member of every non-2xx response."""

    code: ErrorCode
    message: str
    details: dict[str, JsonValue] | None = None


class ErrorBody(_Model):
    """Every non-2xx response body."""

    error: ErrorDetail

    @classmethod
    def of(cls, error: ApiError) -> ErrorBody:
        """Render one ``ApiError``."""
        return cls(error=ErrorDetail(code=error.code, message=error.message, details=error.details))


# Group (a): folders, projects, catalog, auth.


class FsEntry(_Model):
    """One folder the picker can open; ``path`` is canonical (symlinks resolved)."""

    name: str
    path: str
    git: bool


class FsListing(_Model):
    """A folder's subfolders; ``path`` is null when listing the granted roots."""

    path: str | None
    parent: str | None
    entries: list[FsEntry]


class ValidateRequest(_Model):
    """Body of ``POST /api/projects/validate``."""

    path: str


class ProjectState(StrEnum):
    """Readiness of a folder as a VibeSys project, first blocker wins."""

    MISSING = "missing"
    NOT_GIT = "not_git"
    INVALID = "invalid"
    UNINITIALIZED = "uninitialized"
    NO_TASKS = "no_tasks"
    NO_COMMITS = "no_commits"
    DIRTY_TREE = "dirty_tree"
    READY = "ready"


class ProjectRef(_Model):
    """A project the API can address by ``id`` in later URLs."""

    id: str
    root: str
    name: str


class ProjectValidation(_Model):
    """Response of ``POST /api/projects/validate``."""

    state: ProjectState
    path: str
    project: ProjectRef | None = None
    message: str | None = None
    tasks: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


class RecentProject(ProjectRef):
    """One recent-projects entry, most recent first."""

    last_opened: str


class ProjectList(_Model):
    """Response of ``GET /api/projects`` and the on-disk recent-projects file."""

    projects: list[RecentProject]


class DriverOption(_Model):
    """One agent driver and the providers it runs."""

    driver: Driver
    providers: list[str]
    supports_docker: bool


class ProviderOption(_Model):
    """One shipped CLI provider and its model suggestions."""

    provider: str
    display_name: str
    supports_reasoning_effort: bool
    suggested_models: list[str]


class LoopBudget(_Model):
    """The total-budget flag an outer loop takes and its CLI default."""

    flag: Literal["--max-rounds", "--max-generations"]
    default: int


class OuterLoopOption(_Model):
    """One outer loop the start form offers."""

    id: str
    budget: LoopBudget
    requires_profile_guided: bool
    roles: list[str]


class Catalog(_Model):
    """Response of ``GET /api/agents/catalog``."""

    drivers: list[DriverOption]
    providers: list[ProviderOption]
    outer_loops: list[OuterLoopOption]
    compute_backends: list[ComputeBackend]
    default_compute_backend: ComputeBackend


class KeyVar(_Model):
    """Where one allowlisted key variable is set; the value is never returned."""

    name: str
    source: Literal["env", "dotenv", "missing"]
    shadowed: bool


class ProviderAuth(_Model):
    """Sign-in state of one provider."""

    provider: str
    display_name: str
    status: Literal["key", "cli_session", "missing"]
    keys: list[KeyVar]
    cli_session: Literal["present", "absent", "unknown"]
    login_command: str


class AuthStatus(_Model):
    """Response of ``GET /api/auth``."""

    providers: list[ProviderAuth]
    dotenv_path: str


class KeyWrite(_Model):
    """Body of ``PUT /api/auth/{provider}``; ``value`` never leaves the server."""

    name: str
    value: SecretStr


class KeyWriteResult(_Model):
    """Response of ``PUT /api/auth/{provider}``."""

    provider: str
    name: str
    status: Literal["unverified"]
    shadowed_by_env: bool


SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    ErrorBody,
    FsListing,
    ValidateRequest,
    ProjectValidation,
    ProjectList,
    Catalog,
    AuthStatus,
    KeyWrite,
    KeyWriteResult,
)


def main() -> None:
    """Print the JSON Schema of every request and response model."""
    schema = {model.__name__: model.model_json_schema(by_alias=True) for model in SCHEMA_MODELS}
    sys.stdout.write(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Write the request context**

`src/entrypoints/web_home/context.py`:

```python
"""Per-server dependencies and the file primitives the endpoints share."""

from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, JsonValue, ValidationError

from entrypoints.web_home.contract import ApiError, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class HomeConfig:
    """Everything the endpoints read from the host; tests inject each field."""

    state_home: Path
    roots: tuple[Path, ...]
    dotenv_path: Path
    assets_dir: Path | None
    port: int
    dev_origins: tuple[str, ...] = ()
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    clock: Callable[[], datetime] = _utc_now
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # ponytail: one lock serializes every file write; per-file locks if it contends.
    write_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def origin(self) -> str:
        """Return the exact origin the app is served from."""
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class Request:
    """One routed API request."""

    config: HomeConfig
    params: tuple[str, ...]
    query: dict[str, list[str]]
    body: bytes

    def arg(self, name: str) -> str | None:
        """Return the first value of one query parameter."""
        values = self.query.get(name)
        return values[0] if values else None


def validation_errors(error: ValidationError) -> list[JsonValue]:
    """Describe validation failures by location and message, never by input value."""
    return [
        {"loc": [str(part) for part in item["loc"]], "msg": item["msg"]}
        for item in error.errors(include_input=False, include_url=False)
    ]


def parse_body(request: Request, model: type[_ModelT]) -> _ModelT:
    """Validate a JSON body; the error never echoes a submitted value."""
    try:
        return model.model_validate_json(request.body or b"{}")
    except ValidationError as error:
        message = "request body is invalid"
        raise ApiError(
            ErrorCode.INVALID_REQUEST, message, details={"errors": validation_errors(error)}
        ) from None


def atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    """Replace *path* with *data* by one rename; a symlinked *path* is refused."""
    if path.is_symlink():
        message = f"refusing to replace a symlink: {path}"
        raise ApiError(ErrorCode.SYMLINK_REJECTED, message)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
```

- [ ] **Step 6: Write the server**

`src/entrypoints/web_home/app.py`:

```python
"""The `vibesys web home` server: capability checks, routing, assets, and startup."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from urllib.parse import parse_qs, unquote, urlsplit

from entrypoints.web_home.context import HomeConfig, Request
from entrypoints.web_home.contract import ApiError, ErrorBody, ErrorCode
from server.runtime import WebInstanceClaim, WebInstanceRecord
from vibesys.api import DOTENV_PATH
from vs_project.api import state_home

if TYPE_CHECKING:
    import argparse
    import re
    from collections.abc import Callable

    from pydantic import BaseModel

_LOG = logging.getLogger(__name__)
DEFAULT_PORT = 8764
_MAX_BODY_BYTES = 1 << 20
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self' ws://127.0.0.1:*; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
_SECURITY_HEADERS = (
    ("Content-Security-Policy", _CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
_STATE_CHANGING = frozenset({"POST", "PUT", "DELETE"})
_ROUTES: tuple[tuple[str, re.Pattern[str], Callable[[Request], BaseModel]], ...] = ()


def _route(method: str, path: str) -> tuple[Callable[[Request], BaseModel], tuple[str, ...]]:
    for route_method, pattern, handler in _ROUTES:
        match = pattern.fullmatch(path)
        if match is not None and route_method == method:
            return handler, tuple(unquote(group) for group in match.groups())
    message = f"no endpoint for {method} {path}"
    raise ApiError(ErrorCode.NOT_FOUND, message)


class HomeServer(ThreadingHTTPServer):
    """A loopback HTTP server bound to one ``HomeConfig``."""

    daemon_threads = True

    def __init__(self, config: HomeConfig) -> None:
        """Bind 127.0.0.1 on ``config.port`` (0 picks a free port and updates the config)."""
        super().__init__(("127.0.0.1", config.port), _Handler)
        config.port = int(self.server_address[1])
        self.config = config

    @property
    def url(self) -> str:
        """Return the capability URL that opens the app."""
        return f"{self.config.origin}/?token={self.config.token}"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    @override
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        _LOG.info("%s %s %s", self._command(), self._path(), code)

    @override
    def log_message(self, format: str, *args: object) -> None:
        # The raw request line carries the query (a capability token, possibly
        # percent-encoded), so only the method, the path, and the fixed format are logged.
        _LOG.info("%s %s: %s", self._command(), self._path(), format)

    def _command(self) -> str:
        return getattr(self, "command", None) or "-"

    def _path(self) -> str:
        return urlsplit(getattr(self, "path", "") or "").path or "-"

    @property
    def _config(self) -> HomeConfig:
        return cast("HomeServer", self.server).config

    def _dispatch(self, method: str) -> None:
        try:
            self._handle(method)
        except ApiError as error:
            self._send_json(error.status, ErrorBody.of(error))
        except Exception:  # noqa: BLE001  # lint-waiver: LW-101301 [BLE001]; one failed request must answer 500 instead of dropping the connection.
            # > Catching only known types lets an unforeseen error close the socket
            # > with no response; a try block in every endpoint repeats this one.
            _LOG.exception("home request failed: %s %s", method, urlsplit(self.path).path)
            internal = ApiError(ErrorCode.INTERNAL, "internal error")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, ErrorBody.of(internal))

    def _handle(self, method: str) -> None:
        config = self._config
        self._require_host(config)
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if method == "GET" and parsed.path.startswith("/assets/"):
            self._send_asset(config, unquote(parsed.path.removeprefix("/")))
            return
        if method == "GET" and parsed.path in {"/", "/index.html", "/health"}:
            self._require_query_token(config, query)
            if parsed.path == "/health":
                self._send(HTTPStatus.OK, b"vibesys-ok\n", "text/plain")
            else:
                self._send_asset(config, "index.html")
            return
        if not parsed.path.startswith("/api/"):
            message = "not found"
            raise ApiError(ErrorCode.NOT_FOUND, message)
        self._require_bearer_token(config)
        if method in _STATE_CHANGING:
            self._require_origin(config)
        handler, params = _route(method, parsed.path)
        body = self._read_body() if method in {"POST", "PUT"} else b""
        result = handler(Request(config=config, params=params, query=query, body=body))
        self._send_json(HTTPStatus.OK, result)

    def _require_host(self, config: HomeConfig) -> None:
        # One allowlist: the Host of the exact origin the app is served from and Origin checks use.
        if self.headers.get("Host") != config.origin.removeprefix("http://"):
            message = "unexpected Host header"
            raise ApiError(ErrorCode.FORBIDDEN_ORIGIN, message)

    def _require_query_token(self, config: HomeConfig, query: dict[str, list[str]]) -> None:
        token = query.get("token", [""])[0]
        if not secrets.compare_digest(token.encode(), config.token.encode()):
            message = "missing or invalid capability token"
            raise ApiError(ErrorCode.UNAUTHORIZED, message)

    def _require_bearer_token(self, config: HomeConfig) -> None:
        header = self.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        if not secrets.compare_digest(token.encode(), config.token.encode()):
            message = "missing or invalid capability token"
            raise ApiError(ErrorCode.UNAUTHORIZED, message)

    def _require_origin(self, config: HomeConfig) -> None:
        if self.headers.get("Origin") not in {config.origin, *config.dev_origins}:
            message = "state-changing requests must come from the app origin"
            raise ApiError(ErrorCode.FORBIDDEN_ORIGIN, message)

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        if not raw_length.isdigit() or int(raw_length) > _MAX_BODY_BYTES:
            message = f"Content-Length must be an integer of at most {_MAX_BODY_BYTES} bytes"
            raise ApiError(ErrorCode.INVALID_REQUEST, message)
        return self.rfile.read(int(raw_length))

    def _send_asset(self, config: HomeConfig, relative: str) -> None:
        root = config.assets_dir
        if root is None:
            message = "web assets are not built; run `pnpm build` in clients/web"
            raise ApiError(ErrorCode.NOT_FOUND, message)
        allowed = root if relative == "index.html" else root / "assets"
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(allowed) or not candidate.is_file():
            message = "not found"
            raise ApiError(ErrorCode.NOT_FOUND, message)
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)

    def _send_json(self, status: HTTPStatus, model: BaseModel) -> None:
        body = model.model_dump_json(by_alias=True).encode()
        self._send(status, body, "application/json")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _settings_path(web_dir: Path) -> Path:
    return web_dir / "home-settings.json"


def saved_port(web_dir: Path) -> int:
    """Return the saved listen port, or the default."""
    try:
        saved = json.loads(_settings_path(web_dir).read_text(encoding="utf-8"))["port"]
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_PORT
    return saved if isinstance(saved, int) and 0 < saved < 1 << 16 else DEFAULT_PORT


def save_port(web_dir: Path, port: int) -> None:
    """Persist *port* as the default; call only after it bound."""
    web_dir.mkdir(parents=True, exist_ok=True)
    _settings_path(web_dir).write_text(json.dumps({"port": port}) + "\n", encoding="utf-8")


def _announce(url: str, *, open_browser: bool) -> None:
    print(f"VibeSys home: {url}", flush=True)  # noqa: T201  # lint-waiver: LW-101303 [T201]; the capability URL on stdout is the handoff to Electron and to the operator.
    # > Logging would route the URL through handlers that may be redirected or
    # > reformatted; the launcher parses this exact stdout line.
    if open_browser:
        webbrowser.open(url, new=2)


def run_home(args: argparse.Namespace, repository_root: Path) -> int:
    """Serve the app until interrupted; reuse a running home server instead of a second one."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    web_dir = state_home() / "web"
    record_path = web_dir / "home.json"
    existing = WebInstanceRecord.discover(record_path)
    if existing is not None:
        _announce(existing.url, open_browser=args.open)
        return 0
    claim = WebInstanceClaim(record_path)
    if not claim.try_acquire():
        message = "vibesys web home: another home server is starting"
        raise SystemExit(message)
    assets = args.assets or repository_root / "clients" / "web" / "dist"
    config = HomeConfig(
        state_home=web_dir.parent,
        roots=tuple(root.expanduser().resolve() for root in args.root) or (Path.home().resolve(),),
        dotenv_path=DOTENV_PATH,
        assets_dir=assets.resolve() if assets.is_dir() else None,
        port=args.port or saved_port(web_dir),
        dev_origins=tuple(args.dev_origin),
    )
    try:
        server = HomeServer(config)
    except OSError as error:
        claim.close()
        message = (
            f"vibesys web home: cannot listen on {config.origin} ({error.strerror}). "
            "Free the port, or pass --port; a new port changes the app origin, and run "
            "gateways started for the old one reject the app until reopened."
        )
        raise SystemExit(message) from None
    if config.port != saved_port(web_dir):
        save_port(web_dir, config.port)
    record = WebInstanceRecord.from_gateway(
        pid=os.getpid(), port=config.port, token=config.token, project_root=web_dir
    )
    record.write(record_path)
    _announce(server.url, open_browser=args.open)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        record.remove_if_owner(record_path)
        claim.close()
    return 0
```

- [ ] **Step 7: Add the `home` subcommand**

In `src/entrypoints/web.py`, add `from entrypoints.web_home.app import run_home` before `from server.runtime import WebInstanceRecord`. In `_parser()`, before the `stop` parser:

```python
    home = commands.add_parser("home", help="serve the desktop app and its setup API")
    home.add_argument(
        "--port", type=_port, default=None, help="listen port, saved as the new default (8764)"
    )
    home.add_argument(
        "--root",
        type=Path,
        action="append",
        default=[],
        help="folder the picker may browse; repeatable (default: your home directory)",
    )
    home.add_argument(
        "--dev-origin",
        action="append",
        default=[],
        help="extra exact Origin allowed to call the API, e.g. http://127.0.0.1:5173",
    )
    home.add_argument("--assets", type=Path, default=None, help="built app (clients/web/dist)")
    home.add_argument("--open", action="store_true", help="open the app in a browser")
```

In `main()`, before `if args.command == "tunnel":`:

```python
    if args.command == "home":
        return run_home(args, root)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_server.py tests/entrypoints/test_web.py -q --basetemp=/tmp/vsw`
Expected: all pass.

- [ ] **Step 9: Smoke the command**

Run: `uv run python -m entrypoints.web home --port 8799 --root /tmp` and in another shell `curl -s -o /dev/null -w '%{http_code}\n' "$(the printed URL)"` then `curl -s -w '%{http_code}\n' http://127.0.0.1:8799/api/projects`.
Expected: one stdout line `VibeSys home: http://127.0.0.1:8799/?token=...`; `200` (or `404 not_found` if `clients/web/dist` is not built); then `401`. The log shows `GET / 200` with no query. Stop with Ctrl-C; `~/.vibesys/web/home.json` is removed. Re-run without `--port`: it listens on 8799 (saved). Occupy the port (`python3 -m http.server 8799`) and start with `--port 8799`: it exits with the message explaining the origin consequence, and the saved port is unchanged.

- [ ] **Step 10: Commit**

```bash
git add src/entrypoints/web_home src/entrypoints/web.py tests/entrypoints/web_home libs/vs-project/src/vs_project/_state.py libs/vs-project/src/vs_project/api/__init__.py src/vibesys/config.py src/vibesys/api/__init__.py
git commit -m "feat(web): add the vibesys web home server with capability and origin checks"
```

### Task 2: Folder browsing (`GET /api/fs`)

**Files:**
- Create: `src/entrypoints/web_home/projects.py`
- Modify: `src/entrypoints/web_home/app.py` (imports, `_ROUTES`)
- Test: `tests/entrypoints/web_home/test_home_folders.py`

**Interfaces:**
- Consumes: `Request.arg`, `ApiError`, `FsEntry`, `FsListing`, `HomeConfig.roots`.
- Produces: `confine(config, raw: str) -> Path` (absolute, canonical, inside a root; raises `invalid_path`/`outside_roots`), `list_directory(request) -> FsListing`.

- [ ] **Step 1: Write the failing tests**

`tests/entrypoints/web_home/test_home_folders.py`:

```python
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from tests.entrypoints.web_home.support import Home


def test_without_a_path_the_listing_is_the_granted_roots(home: Home) -> None:
    listing = home.get("/api/fs").json()

    assert listing["path"] is None
    assert [entry["path"] for entry in listing["entries"]] == [str(home.workspace)]


def test_listing_shows_visible_subfolders_with_canonical_paths(home: Home) -> None:
    (home.workspace / "b-repo" / ".git").mkdir(parents=True)
    (home.workspace / "a-plain").mkdir()
    (home.workspace / ".hidden").mkdir()
    (home.workspace / "file.txt").write_text("x")

    listing = home.get(f"/api/fs?path={home.workspace}").json()

    assert [(e["name"], e["git"]) for e in listing["entries"]] == [
        ("a-plain", False),
        ("b-repo", True),
    ]
    assert listing["parent"] is None


def test_symlinks_that_leave_the_roots_are_hidden_and_refused(home: Home, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (home.workspace / "escape").symlink_to(outside)
    (home.workspace / "inner").mkdir()
    (home.workspace / "alias").symlink_to(home.workspace / "inner")

    entries = home.get(f"/api/fs?path={home.workspace}").json()["entries"]

    assert [(e["name"], e["path"]) for e in entries] == [
        ("alias", str(home.workspace / "inner")),
        ("inner", str(home.workspace / "inner")),
    ]
    refused = home.get(f"/api/fs?path={home.workspace / 'escape'}").json()
    assert refused["error"]["code"] == "outside_roots"


@pytest.mark.parametrize("path", ["relative/dir", "/", "/etc"])
def test_paths_outside_the_roots_or_relative_are_rejected(home: Home, path: str) -> None:
    code = home.get(f"/api/fs?path={path}").json()["error"]["code"]

    assert code in {"invalid_path", "outside_roots"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unreadable_folder_reports_permission_denied(home: Home) -> None:
    locked = home.workspace / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        reply = home.get(f"/api/fs?path={locked}").json()
    finally:
        locked.chmod(0o755)

    assert reply["error"]["code"] == "permission_denied"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_folders.py -q --basetemp=/tmp/vsw`
Expected: FAIL, responses are `404 not_found` (no route).

- [ ] **Step 3: Implement**

`src/entrypoints/web_home/projects.py`:

```python
"""Folder browsing, project validation, and the recent-projects list."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.web_home.contract import ApiError, ErrorCode, FsEntry, FsListing

if TYPE_CHECKING:
    from entrypoints.web_home.context import HomeConfig, Request


def confine(config: HomeConfig, raw: str) -> Path:
    """Return the canonical form of absolute *raw*, or reject it outside the granted roots.

    Symlinks are resolved before the containment check, so a link inside a
    root that points outside it is rejected like the target itself.
    """
    lexical = Path(raw).expanduser()
    if not raw or "\0" in raw or not lexical.is_absolute():
        message = "path must be an absolute path"
        raise ApiError(ErrorCode.INVALID_PATH, message)
    try:
        path = lexical.resolve()
    except OSError as error:
        message = f"cannot resolve {lexical}: {error.strerror}"
        raise ApiError(ErrorCode.INVALID_PATH, message) from None
    if not _within_roots(config, path):
        message = f"{path} is outside the folders this app may open"
        raise ApiError(ErrorCode.OUTSIDE_ROOTS, message)
    return path


def _within_roots(config: HomeConfig, path: Path) -> bool:
    return any(path.is_relative_to(root) for root in config.roots)


def list_directory(request: Request) -> FsListing:
    """``GET /api/fs?path=``: subfolders of *path*, or the granted roots without one."""
    config = request.config
    raw = request.arg("path")
    if raw is None:
        return FsListing(
            path=None,
            parent=None,
            entries=[
                FsEntry(name=str(root), path=str(root), git=_is_git(root)) for root in config.roots
            ],
        )
    directory = confine(config, raw)
    if not directory.is_dir():
        message = f"not a directory: {directory}"
        raise ApiError(ErrorCode.INVALID_PATH, message)
    try:
        children = sorted(directory.iterdir(), key=lambda child: child.name.lower())
    except PermissionError:
        message = f"permission denied: {directory}"
        raise ApiError(ErrorCode.PERMISSION_DENIED, message) from None
    parent = directory.parent
    return FsListing(
        path=str(directory),
        parent=str(parent) if parent != directory and _within_roots(config, parent) else None,
        entries=[entry for child in children if (entry := _entry(config, child)) is not None],
    )


def _entry(config: HomeConfig, child: Path) -> FsEntry | None:
    if child.name.startswith("."):
        return None
    try:
        target = child.resolve(strict=True)
        if not target.is_dir() or not _within_roots(config, target):
            return None
        return FsEntry(name=child.name, path=str(target), git=_is_git(target))
    except OSError:
        return None


def _is_git(path: Path) -> bool:
    try:
        return (path / ".git").exists()
    except OSError:
        return False
```

In `app.py` add `from entrypoints.web_home import projects`, move `import re` from the `TYPE_CHECKING` block to the module imports, and replace `_ROUTES = ()` with:

```python
_ROUTES: tuple[tuple[str, re.Pattern[str], Callable[[Request], BaseModel]], ...] = (
    ("GET", re.compile(r"/api/fs"), projects.list_directory),
)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_folders.py -q --basetemp=/tmp/vsw`
Expected: PASS (the permission test is skipped when run as root).

- [ ] **Step 5: Commit**

```bash
git add src/entrypoints/web_home tests/entrypoints/web_home/test_home_folders.py
git commit -m "feat(web): browse folders confined to the granted roots"
```

### Task 3: Project validation and recent projects

**Files:**
- Modify: `src/entrypoints/web_home/context.py` (add `git`, `pending_changes`), `src/entrypoints/web_home/projects.py`, `src/entrypoints/web_home/app.py` (routes)
- Modify: `tests/entrypoints/web_home/support.py` (add `MANIFEST`, `make_project`, `project_key`)
- Test: `tests/entrypoints/web_home/test_home_projects.py`

**Interfaces:**
- Produces: `git(root, *args) -> CompletedProcess[str]`; `pending_changes(root) -> list[str]` (same query as `GitTracker.pending_changes`); `project_id(root) -> str`; `inspect_project(path) -> ProjectValidation`; `remember(config, ProjectRef)`; `resolve_project(config, key) -> Path` (raises `unknown_project`); test helpers `make_project(root, *, tasks=("bench",), commit=True) -> Path` and `project_key(home, root) -> str`.

- [ ] **Step 1: Extend the test support**

Append to `tests/entrypoints/web_home/support.py` (and add `from tests.support import run_test_command` to its imports):

```python
MANIFEST = """version = 1

[agent]
domain = "generic"

[accuracy]
command = ["python", "check.py"]

[benchmark]
command = ["python", "bench.py"]

[benchmark.result]
json_argument = "--json"
metric = "throughput"
"""


def make_project(root: Path, *, tasks: tuple[str, ...] = ("bench",), commit: bool = True) -> Path:
    """Create a git work tree with repository-native tasks, committed unless told not to."""
    root.mkdir(parents=True, exist_ok=True)
    run_test_command(["git", "init", "-q"], cwd=root, check=True)
    run_test_command(["git", "config", "user.name", "Test"], cwd=root, check=True)
    run_test_command(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    run_test_command(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    for name in tasks:
        task = root / ".vibesys" / "tasks" / name
        task.mkdir(parents=True)
        (task / "OBJECTIVE.md").write_text(f"Make {name} faster.\n")
        (task / "vibesys.input.toml").write_text(MANIFEST)
    (root / "README.md").write_text("project\n")
    if commit:
        run_test_command(["git", "add", "-A"], cwd=root, check=True)
        run_test_command(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root.resolve()


def project_key(home: Home, root: Path) -> str:
    """Validate *root* through the API and return its project id."""
    reply = home.post("/api/projects/validate", {"path": str(root)})
    assert reply.status == 200, reply.body
    return reply.json()["project"]["id"]
```

- [ ] **Step 2: Write the failing tests**

`tests/entrypoints/web_home/test_home_projects.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support import run_test_command

from tests.entrypoints.web_home.support import make_project

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home


def _state(home: Home, path: object) -> str:
    return home.post("/api/projects/validate", {"path": str(path)}).json()["state"]


def test_validation_reports_the_first_blocker_in_order(home: Home) -> None:
    root = home.workspace / "p"
    assert _state(home, root) == "missing"
    root.mkdir()
    assert _state(home, root) == "not_git"
    run_test_command(["git", "init", "-q"], cwd=root, check=True)
    assert _state(home, root) == "uninitialized"
    (root / ".vibesys" / "tasks").mkdir(parents=True)
    assert _state(home, root) == "no_tasks"


def test_validation_distinguishes_commitless_dirty_and_ready(home: Home) -> None:
    assert _state(home, make_project(home.workspace / "fresh", commit=False)) == "no_commits"
    ready = make_project(home.workspace / "ready")
    assert _state(home, ready) == "ready"
    (ready / "scratch.txt").write_text("x")

    reply = home.post("/api/projects/validate", {"path": str(ready)}).json()

    assert (reply["state"], reply["pending"], reply["tasks"]) == (
        "dirty_tree",
        ["scratch.txt"],
        ["bench"],
    )


def test_a_symlinked_config_root_is_invalid(home: Home) -> None:
    root = make_project(home.workspace / "linked", tasks=())
    target = home.workspace / "elsewhere"
    target.mkdir()
    (root / ".vibesys").symlink_to(target)

    reply = home.post("/api/projects/validate", {"path": str(root)}).json()

    assert reply["state"] == "invalid"
    assert "symlink" in reply["message"]


def test_validated_git_work_trees_become_recent_projects(home: Home) -> None:
    first = make_project(home.workspace / "first")
    second = make_project(home.workspace / "second")
    (home.workspace / "plain").mkdir()
    for path in (first, second, home.workspace / "plain", first):
        home.post("/api/projects/validate", {"path": str(path)})

    projects = home.get("/api/projects").json()["projects"]

    assert [p["root"] for p in projects] == [str(first), str(second)]
    assert projects[0]["last_opened"] == "2026-09-28T12:00:00+00:00"


def test_validate_rejects_paths_outside_the_roots(home: Home) -> None:
    reply = home.post("/api/projects/validate", {"path": "/"})

    assert (reply.status, reply.json()["error"]["code"]) == (403, "outside_roots")
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_projects.py -q --basetemp=/tmp/vsw`
Expected: FAIL with `not_found` for `/api/projects/validate`.

- [ ] **Step 4: Add the git primitives**

In `context.py`, change the module docstring to `"""Per-server dependencies and the file and git primitives the endpoints share."""`, add `import shutil` and `import subprocess` to the imports, add these constants after `_ModelT`:

```python
_GIT_TIMEOUT_SECONDS = 60
_GIT_OVERRIDES = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"})
_PORCELAIN_STATUS_WIDTH = len("XY ")
```

and append:

```python
def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in *root* without a shell or inherited repository overrides."""
    executable = shutil.which("git")
    if executable is None:
        message = "git is not installed"
        raise ApiError(ErrorCode.NOT_GIT, message)
    environment = {key: value for key, value in os.environ.items() if key not in _GIT_OVERRIDES}
    return subprocess.run(  # noqa: S603  # lint-waiver: LW-101302 [S603]; git argv is built here from fixed subcommands and validated paths, never a shell string.
        # > Routing through GitTracker needs a run id and state integration the
        # > setup API does not have; shell=True would weaken argv safety.
        [executable, *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def pending_changes(root: Path) -> list[str]:
    """Return changed and untracked paths under *root*, relative to it.

    Same query as ``GitTracker.pending_changes``, which launch uses to reject a
    dirty tree, so the setup API and launch agree on what "dirty" means.
    """
    prefix = git(root, "rev-parse", "--show-prefix").stdout.strip()
    records = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".")
    fields = records.stdout.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) <= _PORCELAIN_STATUS_WIDTH:
            continue
        paths.append(record[_PORCELAIN_STATUS_WIDTH:].removeprefix(prefix))
        if record[0] in "RC":
            index += 1
    return sorted(paths)
```

- [ ] **Step 5: Implement validation and recents**

In `projects.py`, add `import hashlib`, `from pydantic import ValidationError`, `from entrypoints.web_home.context import atomic_write, git, parse_body, pending_changes`, `from vs_project.api import Project, ProjectError`, and `ProjectList`, `ProjectRef`, `ProjectState`, `ProjectValidation`, `RecentProject`, `ValidateRequest` to the contract import. Add `_MAX_RECENTS = 20` after the imports and append:

```python
def project_id(root: Path) -> str:
    """Return the stable URL id of a canonical project root."""
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


def inspect_project(path: Path) -> ProjectValidation:
    """Classify *path* by its first launch blocker, in the order the UI resolves them."""
    if not path.is_dir():
        return ProjectValidation(state=ProjectState.MISSING, path=str(path))
    if git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return ProjectValidation(state=ProjectState.NOT_GIT, path=str(path))
    ref = ProjectRef(id=project_id(path), root=str(path), name=path.name)
    try:
        project = Project.open(path)
        initialized = project.is_initialized()
        tasks = [task.name.value for task in project.discover_tasks()] if initialized else []
    except ProjectError as error:
        return ProjectValidation(
            state=ProjectState.INVALID, path=str(path), project=ref, message=str(error)
        )
    has_commits = git(path, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0
    pending = pending_changes(path) if has_commits else []
    if not initialized:
        state = ProjectState.UNINITIALIZED
    elif not tasks:
        state = ProjectState.NO_TASKS
    elif not has_commits:
        state = ProjectState.NO_COMMITS
    elif pending:
        state = ProjectState.DIRTY_TREE
    else:
        state = ProjectState.READY
    return ProjectValidation(state=state, path=str(path), project=ref, tasks=tasks, pending=pending)


def validate(request: Request) -> ProjectValidation:
    """``POST /api/projects/validate``: classify a folder and remember git work trees."""
    body = parse_body(request, ValidateRequest)
    validation = inspect_project(confine(request.config, body.path))
    if validation.project is not None:
        remember(request.config, validation.project)
    return validation


def _recents_path(config: HomeConfig) -> Path:
    return config.state_home / "web" / "recent-projects.json"


def _load_recents(config: HomeConfig) -> list[RecentProject]:
    try:
        return ProjectList.model_validate_json(_recents_path(config).read_bytes()).projects
    except (OSError, ValidationError):
        return []


def remember(config: HomeConfig, project: ProjectRef) -> None:
    """Move *project* to the front of the recent-projects list."""
    entry = RecentProject(**project.model_dump(), last_opened=config.clock().isoformat())
    with config.write_lock:
        kept = [item for item in _load_recents(config) if item.id != project.id]
        document = ProjectList(projects=[entry, *kept][:_MAX_RECENTS])
        atomic_write(_recents_path(config), document.model_dump_json(indent=2).encode(), mode=0o600)


def recent(request: Request) -> ProjectList:
    """``GET /api/projects``: recent projects, most recent first."""
    return ProjectList(projects=_load_recents(request.config))


def resolve_project(config: HomeConfig, key: str) -> Path:
    """Return the canonical root of a remembered project id, still inside the roots."""
    for entry in _load_recents(config):
        if entry.id == key:
            return confine(config, entry.root)
    message = f"unknown project id {key!r}; validate the folder first"
    raise ApiError(ErrorCode.UNKNOWN_PROJECT, message)
```

Add to `_ROUTES` in `app.py`:

```python
    ("POST", re.compile(r"/api/projects/validate"), projects.validate),
    ("GET", re.compile(r"/api/projects"), projects.recent),
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/entrypoints/web_home tests/entrypoints/web_home
git commit -m "feat(web): validate project folders and remember recent projects"
```

### Task 4: Agent catalog

**Files:**
- Modify: `libs/vs-agent/src/vs_agent/provider_policy.py`, `libs/vs-agent/src/vs_agent/api/__init__.py`, `src/vibesys/api/__init__.py`, `src/vibesys/api/request.py`, `src/server/chat/options.py:45-72,110`
- Create: `src/entrypoints/web_home/catalog.py`
- Modify: `src/entrypoints/web_home/app.py`
- Test: `tests/entrypoints/web_home/test_home_catalog.py`; existing `tests/server/test_chat_manager.py` must stay green

**Interfaces:**
- Produces: `vs_agent.api.SUGGESTED_MODELS: dict[str, tuple[str, ...]]` (re-exported by `vibesys.api`); `vibesys.api.request.orchestration_roles(orchestration_id) -> tuple[str, ...]`; `catalog.LOOPS: dict[str, tuple[BudgetFlag, parser_factory, orchestration_id]]`; `catalog.budget_destination(flag) -> str`; `catalog.host_compute_backend() -> ComputeBackend`.

- [ ] **Step 1: Write the failing tests**

`tests/entrypoints/web_home/test_home_catalog.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from entrypoints.cli.constants import _OUTER_LOOPS
from vibesys.api import ComputeBackend
from vs_agent.api import SHIPPED_PROVIDERS

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home


def test_catalog_offers_every_cli_loop_with_its_budget_flag(home: Home) -> None:
    catalog = home.get("/api/agents/catalog").json()
    loops = {loop["id"]: loop for loop in catalog["outer_loops"]}

    assert list(loops) == list(_OUTER_LOOPS)
    assert loops["agent"]["budget"] == {"flag": "--max-rounds", "default": 24}
    assert loops["plain"]["budget"] == {"flag": "--max-rounds", "default": 5}
    assert loops["evolve"]["budget"] == {"flag": "--max-generations", "default": 8}
    assert loops["dynamic"]["budget"]["flag"] == "--max-rounds"
    assert loops["profile-guided"]["requires_profile_guided"] is True
    assert "implementer" in loops["agent"]["roles"]


def test_catalog_lists_shipped_providers_drivers_and_backends(home: Home) -> None:
    catalog = home.get("/api/agents/catalog").json()

    assert [p["provider"] for p in catalog["providers"]] == list(SHIPPED_PROVIDERS)
    codex = next(p for p in catalog["providers"] if p["provider"] == "codex")
    assert "gpt-5.5" in codex["suggested_models"]
    assert {d["driver"] for d in catalog["drivers"]} == {"agentshim", "omnigent"}
    assert catalog["compute_backends"] == [b.value for b in ComputeBackend]
    assert catalog["default_compute_backend"] in catalog["compute_backends"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_catalog.py -q --basetemp=/tmp/vsw`
Expected: FAIL, `not_found`.

- [ ] **Step 3: Move the model suggestions to the provider policy**

In `libs/vs-agent/src/vs_agent/provider_policy.py`, after `DEFAULT_CLI_PROVIDER` and its docstring, add:

```python
OPENCODE_DEFAULT_MODEL = "google-vertex/gemini-3-pro-preview"
"""The opencode model VibeSys offers first (a deployment default, not the library's)."""

SUGGESTED_MODELS: dict[str, tuple[str, ...]] = {
    "codex": ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5"),
    "claude": (
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ),
    "gemini": (),
    "opencode": (OPENCODE_DEFAULT_MODEL,),
}
"""A short model suggestion list per shipped provider, not a registry.

The model a run uses is whatever the provider's CLI accepts; clients keep
free-text entry for anything not named here. The codex and claude slugs mirror
the release-curated alias catalogs those CLIs ship (``omnigent.model_fallbacks``),
duplicated because ``omnigent`` is an optional extra. Gemini ships no curated
list, so it offers free-text entry alone.
"""
```

Export `SUGGESTED_MODELS` from `vs_agent/api/__init__.py` (import list after `SHIPPED_PROVIDERS`, `__all__` after `"SHIPPED_PROVIDERS"`). In `src/vibesys/api/__init__.py` change `from vs_agent.api import AgentBackend, AgentSpec` to `from vs_agent.api import SUGGESTED_MODELS, AgentBackend, AgentSpec` and add `"SUGGESTED_MODELS",` to `__all__` after `"KNOWN_COMPUTE_BACKENDS"`.

In `src/server/chat/options.py`, delete `_OPENCODE_DEFAULT_MODEL`, `_SUGGESTED_MODELS`, and their comments, add `from vibesys.api import SUGGESTED_MODELS` after the pydantic import, and replace `_SUGGESTED_MODELS.get(provider, ())` with `SUGGESTED_MODELS.get(provider, ())`.

- [ ] **Step 4: Expose orchestration roles**

In `src/vibesys/api/request.py`, add before `def supported_profilers(` and add `"orchestration_roles",` to `__all__` after `"make_run_environment_spec",`:

```python
def orchestration_roles(orchestration_id: str) -> tuple[str, ...]:
    """Return the agent role IDs a built-in orchestration declares, in order."""
    # lint-waiver: LW-101320 [PLC0415]; the product catalog imports every built-in policy, so it loads only when a caller needs it.
    # > Module scope would import every policy whenever the request facade loads;
    # > a shared cached loader adds indirection for two call sites.
    from vibesys.plugin_builtins import built_in_orchestrations  # noqa: PLC0415

    plugin = built_in_orchestrations().resolve(orchestration_id).plugin
    return tuple(str(role.id) for role in plugin.agents)
```

- [ ] **Step 5: Implement the catalog**

`src/entrypoints/web_home/catalog.py`:

```python
"""The launch options the start form offers: drivers, providers, loops, backends."""

from __future__ import annotations

import platform
import shutil
import sys
from typing import TYPE_CHECKING

from entrypoints.cli.args import _build_agent_parser, _build_evolve_parser, _build_plain_parser
from entrypoints.cli.constants import _OUTER_LOOPS
from entrypoints.web_home.contract import (
    Catalog,
    DriverOption,
    LoopBudget,
    OuterLoopOption,
    ProviderOption,
)
from vibesys.api import SUGGESTED_MODELS, ComputeBackend
from vibesys.api.request import orchestration_roles
from vs_agent.api import SHIPPED_PROVIDERS, agent_catalog, provider_profile

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from typing import Literal

    from entrypoints.web_home.context import Request

    BudgetFlag = Literal["--max-rounds", "--max-generations"]

# Outer loop -> (total-budget flag, parser that owns its default, orchestration whose roles
# the form offers). profile-guided and dynamic parse with the agent parser, so they take
# --max-rounds too (`entrypoints/cli/__init__.py` loop table).
LOOPS: dict[str, tuple[BudgetFlag, Callable[[], argparse.ArgumentParser], str]] = {
    "agent": ("--max-rounds", _build_agent_parser, "multi-agent"),
    "profile-guided": ("--max-rounds", _build_agent_parser, "profile-guided-multi-agent"),
    "dynamic": ("--max-rounds", _build_agent_parser, "dynamic"),
    "plain": ("--max-rounds", _build_plain_parser, "plain"),
    "evolve": ("--max-generations", _build_evolve_parser, "evolve"),
}


def budget_destination(flag: str) -> str:
    """Return the argparse destination (and descriptor option) of a budget flag."""
    return flag.removeprefix("--").replace("-", "_")


def host_compute_backend() -> ComputeBackend:
    """Return the compute backend this machine most likely has."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return ComputeBackend.METAL
    if shutil.which("nvidia-smi"):
        return ComputeBackend.CUDA
    if shutil.which("rocm-smi"):
        return ComputeBackend.ROCM
    return ComputeBackend.CPU


def _loop(loop_id: str) -> OuterLoopOption:
    flag, parser, orchestration = LOOPS[loop_id]
    return OuterLoopOption(
        id=loop_id,
        budget=LoopBudget(flag=flag, default=parser().get_default(budget_destination(flag))),
        requires_profile_guided=loop_id == "profile-guided",
        roles=list(orchestration_roles(orchestration)),
    )


def _provider(name: str) -> ProviderOption:
    profile = provider_profile(name)
    return ProviderOption(
        provider=name,
        display_name=profile.display_name,
        supports_reasoning_effort=profile.supports_reasoning_effort,
        suggested_models=list(SUGGESTED_MODELS.get(name, ())),
    )


def get_catalog(request: Request) -> Catalog:
    """``GET /api/agents/catalog``."""
    del request
    return Catalog(
        drivers=[
            DriverOption(
                driver=driver, providers=list(info.providers), supports_docker=info.supports_docker
            )
            for driver, info in agent_catalog().items()
        ],
        providers=[_provider(name) for name in SHIPPED_PROVIDERS],
        outer_loops=[_loop(loop_id) for loop_id in _OUTER_LOOPS],
        compute_backends=list(ComputeBackend),
        default_compute_backend=host_compute_backend(),
    )
```

In `app.py`, change the import to `from entrypoints.web_home import catalog, projects` and add the route `("GET", re.compile(r"/api/agents/catalog"), catalog.get_catalog),`.

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_catalog.py tests/server/test_chat_manager.py tests/server/test_chat_factory.py -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add libs/vs-agent/src/vs_agent src/vibesys/api src/server/chat/options.py src/entrypoints/web_home tests/entrypoints/web_home/test_home_catalog.py
git commit -m "feat(web): serve the agent catalog; share model suggestions with chat"
```

### Task 5: Provider auth status and write-only keys

**Files:**
- Modify: `libs/vs-agent/src/vs_agent/provider_profiles.py`, `libs/vs-agent/src/vs_agent/host_resource_declarations.py:221-243`, `libs/vs-agent/src/vs_agent/api/__init__.py`, `src/entrypoints/web_home/context.py`, `tests/entrypoints/web_home/conftest.py`
- Create: `src/entrypoints/web_home/keys.py`
- Modify: `src/entrypoints/web_home/app.py`
- Test: `tests/entrypoints/web_home/test_home_auth.py`; existing `libs/vs-agent/tests` stay green

**Interfaces:**
- Produces: `context.keychain_has(service) -> bool | None` and `HomeConfig.keychain` (injectable); `vs_agent.api.provider_profile(name) -> ProviderProfile`; `vs_agent.api.credential_path(profile, *, home, env) -> Path | None`; `provider_profiles.state_dir_path(profile, state_dir, *, home, env) -> Path`; `keys.key_variables(profile) -> tuple[str, ...]`; `keys.store_key(path, name, value)`.

- [ ] **Step 1: Write the failing tests**

`tests/entrypoints/web_home/test_home_auth.py`:

```python
from __future__ import annotations

import ast
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from dotenv import dotenv_values
from hypothesis import given
from hypothesis import strategies as st

from entrypoints.web_home.keys import store_key

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home

SAMPLE_KEY = "sk-test-0123456789"


def _provider(home: Home, name: str) -> dict[str, Any]:
    providers = home.get("/api/auth").json()["providers"]
    return next(p for p in providers if p["provider"] == name)


def test_status_reports_sources_without_values(home: Home) -> None:
    home.config.dotenv_path.parent.mkdir(parents=True)
    home.config.dotenv_path.write_text(f"ANTHROPIC_API_KEY={SAMPLE_KEY}\n")
    home.config.environ = {**home.config.environ, "OPENAI_API_KEY": "sk-env"}

    reply = home.get("/api/auth")
    claude = _provider(home, "claude")
    codex = _provider(home, "codex")

    assert SAMPLE_KEY.encode() not in reply.body
    assert b"sk-env" not in reply.body
    assert claude["status"] == "key"
    assert {"name": "ANTHROPIC_API_KEY", "source": "dotenv", "shadowed": False} in claude["keys"]
    assert codex["keys"] == [{"name": "OPENAI_API_KEY", "source": "env", "shadowed": False}]
    assert _provider(home, "opencode")["keys"] == []


def test_a_cli_session_honors_the_state_root_variable(home: Home, tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    home.config.environ = {**home.config.environ, "CODEX_HOME": str(codex_home)}

    codex = _provider(home, "codex")

    assert (codex["status"], codex["cli_session"], codex["login_command"]) == (
        "cli_session",
        "present",
        "codex login",
    )


@pytest.mark.parametrize(
    ("found", "status", "session"),
    [(True, "cli_session", "present"), (False, "missing", "absent"), (None, "missing", "unknown")],
)
def test_claude_sign_in_is_read_from_the_keychain_by_presence_only(
    home: Home, *, found: bool | None, status: str, session: str
) -> None:
    asked: list[str] = []

    def keychain(service: str) -> bool | None:
        asked.append(service)
        return found

    home.config.keychain = keychain

    claude = _provider(home, "claude")

    assert (claude["status"], claude["cli_session"]) == (status, session)
    assert asked == ["Claude Code-credentials"]


def test_a_credentials_file_wins_without_asking_the_keychain(home: Home) -> None:
    credentials = Path(home.config.environ["HOME"]) / ".claude" / ".credentials.json"
    credentials.parent.mkdir()
    credentials.write_text("{}")
    asked: list[str] = []
    home.config.keychain = lambda service: asked.append(service) or None

    assert _provider(home, "claude")["cli_session"] == "present"
    assert asked == []


def test_writing_a_key_is_write_only_private_and_preserves_other_entries(home: Home) -> None:
    path = home.config.dotenv_path
    path.parent.mkdir(parents=True)
    path.write_text("# keep me\nOTHER=1\nOPENAI_API_KEY=old\n")

    reply = home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY})

    assert reply.status == 200
    assert SAMPLE_KEY.encode() not in reply.body
    assert reply.json() == {
        "provider": "codex",
        "name": "OPENAI_API_KEY",
        "status": "unverified",
        "shadowed_by_env": False,
    }
    assert path.read_text() == f"# keep me\nOTHER=1\nOPENAI_API_KEY='{SAMPLE_KEY}'\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _provider(home, "codex")["status"] == "key"


@pytest.mark.parametrize("inherited", ["sk-stale", ""])
def test_an_inherited_variable_shadows_the_saved_key_even_when_empty(
    home: Home, inherited: str
) -> None:
    home.config.environ = {**home.config.environ, "OPENAI_API_KEY": inherited}

    reply = home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY}).json()
    [key] = _provider(home, "codex")["keys"]

    assert reply["shadowed_by_env"] is True
    assert key == {
        "name": "OPENAI_API_KEY",
        "source": "env" if inherited else "missing",
        "shadowed": True,
    }


@pytest.mark.parametrize(
    ("provider", "body", "code"),
    [
        ("nope", {"name": "X_API_KEY", "value": SAMPLE_KEY}, "unknown_provider"),
        ("codex", {"name": "OPENAI_BASE_URL", "value": SAMPLE_KEY}, "invalid_key"),
        ("codex", {"name": "PATH", "value": SAMPLE_KEY}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "   "}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "sk\nEVIL=1"}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "sk'x"}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "sk${HOME}"}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": 'sk"x'}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY, "extra": 1}, "invalid_request"),
        ("codex", {"name": "OPENAI_API_KEY", "value": 12345}, "invalid_request"),
    ],
)
def test_rejected_keys_never_echo_the_value_or_touch_the_file(
    home: Home, provider: str, body: dict[str, object], code: str
) -> None:
    reply = home.put(f"/api/auth/{provider}", body)

    assert reply.json()["error"]["code"] == code
    assert SAMPLE_KEY.encode() not in reply.body
    assert b"12345" not in reply.body
    assert not home.config.dotenv_path.exists()


def test_a_symlinked_env_file_is_refused(home: Home, tmp_path: Path) -> None:
    target = tmp_path / "real.env"
    target.write_text("A=1\n")
    home.config.dotenv_path.parent.mkdir(parents=True)
    home.config.dotenv_path.symlink_to(target)

    reply = home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY})

    assert reply.json()["error"]["code"] == "symlink_rejected"
    assert target.read_text() == "A=1\n"


def test_a_multiline_value_that_mentions_the_key_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = 'CERT="line one\nOPENAI_API_KEY=not-a-binding\n"\nOPENAI_API_KEY=old\n'
    path.write_text(original)

    store_key(path, "OPENAI_API_KEY", SAMPLE_KEY)

    assert (
        path.read_text()
        == 'CERT="line one\nOPENAI_API_KEY=not-a-binding\n"\n' + f"OPENAI_API_KEY='{SAMPLE_KEY}'\n"
    )


def test_crlf_files_keep_their_line_endings(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"# windows\r\nOTHER=1\r\nOPENAI_API_KEY=old\r\n")

    store_key(path, "OPENAI_API_KEY", SAMPLE_KEY)

    assert (
        path.read_bytes()
        == b"# windows\r\nOTHER=1\r\n" + f"OPENAI_API_KEY='{SAMPLE_KEY}'\n".encode()
    )


def test_writing_a_key_leaves_this_process_environment_alone(home: Home) -> None:
    before = dict(os.environ)
    home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY})

    assert dict(os.environ) == before


def test_the_home_server_never_imports_the_dotenv_loaders() -> None:
    package = Path(__file__).parents[3] / "src" / "entrypoints" / "web_home"
    imported = {
        alias.name
        for path in package.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert imported.isdisjoint({"load_dotenv", "load_config", "find_dotenv"})


_NAMES = st.from_regex(r"[A-Z][A-Z0-9_]{0,8}", fullmatch=True).filter(
    lambda n: n != "OPENAI_API_KEY"
)
_VALUES = st.text(
    st.characters(codec="utf-8", exclude_categories=("Cc", "Cs"), exclude_characters="'\\\"$ #\n"),
    min_size=1,
    max_size=20,
)
_KEY = st.text(
    st.characters(codec="utf-8", exclude_categories=("Cc", "Cs"), exclude_characters="'\"\\$"),
    min_size=1,
    max_size=40,
).filter(str.strip)


@given(entries=st.dictionaries(_NAMES, _VALUES, max_size=5), key=_KEY)
def test_storing_a_key_round_trips_and_keeps_every_other_entry(
    tmp_path_factory: pytest.TempPathFactory, entries: dict[str, str], key: str
) -> None:
    path = tmp_path_factory.mktemp("env") / ".env"
    path.write_text("".join(f"{name}={value}\n" for name, value in entries.items()))

    store_key(path, "OPENAI_API_KEY", key)

    assert dotenv_values(path) == {**entries, "OPENAI_API_KEY": key}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_auth.py -q --basetemp=/tmp/vsw`
Expected: collection error, `No module named 'entrypoints.web_home.keys'`.

- [ ] **Step 3: One source for relocated provider state**

In `libs/vs-agent/src/vs_agent/provider_profiles.py`, add `from pathlib import Path` and, under `TYPE_CHECKING`, `from collections.abc import Mapping`; append:

```python
def state_dir_path(
    profile: ProviderProfile, state_dir: str, *, home: Path, env: Mapping[str, str]
) -> Path:
    """Return where *state_dir* lives, honoring the CLI's own relocation variable.

    ``profile.state_root_env`` (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``) relocates
    only ``state_dirs[0]``, and only when *env* sets it; everything else stays
    under *home*.
    """
    if (
        profile.state_root_env
        and state_dir == profile.state_dirs[0]
        and profile.state_root_env in env
    ):
        return Path(env[profile.state_root_env]).expanduser()
    return home / state_dir


def credential_path(profile: ProviderProfile, *, home: Path, env: Mapping[str, str]) -> Path | None:
    """Return the primary credential file, ``auth_files[0]``, or ``None`` when there is none."""
    if not profile.auth_files:
        return None
    credential = Path(profile.auth_files[0])
    for state_dir in profile.state_dirs:
        if credential.is_relative_to(state_dir):
            root = state_dir_path(profile, state_dir, home=home, env=env)
            return root / credential.relative_to(state_dir)
    return home / credential
```

Replace the body of `_state_root` in `host_resource_declarations.py` (keep its signature) with one delegating line, so the relocation rule has one definition:

```python
def _state_root(
    state_dir: str, *, home: Path, ctx: HostResourceContext, profile: ProviderProfile
) -> Path:
    """Return where *state_dir* actually lives; see ``provider_profiles.state_dir_path``."""
    return provider_profiles.state_dir_path(profile, state_dir, home=home, env=ctx.env)
```

In `vs_agent/api/__init__.py` add `from vs_agent.provider_profiles import credential_path, provider_profile` after the `provider_policy` import and `"credential_path",` / `"provider_profile",` to `__all__` (sorted: after `"cli_skill_dirs"` and after `"expose_as_tools"`).

- [ ] **Step 4: Add the keychain probe**

Claude Code on macOS stores its login in the keychain, not in `.credentials.json`. The probe asks for the item's presence without `-w`, so the secret is never read; the injected `HomeConfig.keychain` is the seam the tests stub. In `context.py` add `import sys`, the constants

```python
_KEYCHAIN_TIMEOUT_SECONDS = 2
_KEYCHAIN_ITEM_NOT_FOUND = 44
```

this function after `_utc_now`:

```python
def keychain_has(service: str) -> bool | None:
    """Return whether the macOS keychain holds a *service* item, without reading its secret.

    ``None`` means unknown: not macOS, no ``security`` tool, a timeout, or an
    unexpected status. Without ``-w`` the tool prints attributes only, and the
    output is discarded.
    """
    executable = shutil.which("security") if sys.platform == "darwin" else None
    if executable is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603  # lint-waiver: LW-101307 [S603]; fixed `security` lookup argv with a constant service name, never a shell string.
            # > A keychain binding would be a new dependency for one presence check;
            # > shell=True would weaken argv safety.
            [executable, "find-generic-password", "-s", service],
            capture_output=True,
            timeout=_KEYCHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    return False if result.returncode == _KEYCHAIN_ITEM_NOT_FOUND else None
```

and the field `keychain: Callable[[str], bool | None] = keychain_has` after `clock` in `HomeConfig`. In `tests/entrypoints/web_home/conftest.py` add `keychain=lambda _service: False,` to the `HomeConfig(...)` call so no test asks the real keychain.

- [ ] **Step 5: Implement keys**

`src/entrypoints/web_home/keys.py`:

```python
"""Provider sign-in state and write-only API keys in the checkout's `.env`.

The home server never loads `.env` into its own environment (no `load_config`,
no `load_dotenv`): it reads the file with `dotenv_values`, so a key saved here
reaches the next launched run instead of being shadowed by a stale copy in
this process.
"""

from __future__ import annotations

import io
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from entrypoints.web_home.context import atomic_write, parse_body
from entrypoints.web_home.contract import (
    ApiError,
    AuthStatus,
    ErrorCode,
    KeyVar,
    KeyWrite,
    KeyWriteResult,
    ProviderAuth,
)
from vs_agent.api import SHIPPED_PROVIDERS, credential_path, provider_profile

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentshim import ProviderProfile

    from entrypoints.web_home.context import HomeConfig, Request

# Only secrets are writable: the profile's other auth variables (base URLs, headers) are not keys.
_KEY_SUFFIXES = ("_API_KEY", "_AUTH_TOKEN")
_LOGIN_COMMANDS = {"codex": "codex login", "opencode": "opencode auth login"}
# Claude Code on macOS keeps its login in the keychain instead of `.credentials.json`.
_KEYCHAIN_SERVICES = {"claude": "Claude Code-credentials"}


def key_variables(profile: ProviderProfile) -> tuple[str, ...]:
    """Return the allowlisted key variables of one provider, in profile order."""
    return tuple(name for name in profile.auth_env_vars if name.endswith(_KEY_SUFFIXES))


def _stored(path: Path) -> dict[str, str | None]:
    return dict(dotenv_values(path)) if path.is_file() else {}


def _key_var(name: str, environ: Mapping[str, str], stored: Mapping[str, str | None]) -> KeyVar:
    # load_dotenv(override=False) skips any name already in the environment, even an empty one,
    # so membership, not truthiness, decides what a launched run sees.
    inherited = name in environ
    if inherited:
        source = "env" if environ[name] else "missing"
    else:
        source = "dotenv" if stored.get(name) else "missing"
    return KeyVar(name=name, source=source, shadowed=inherited and name in stored)


def _cli_session(
    profile: ProviderProfile, config: HomeConfig
) -> Literal["present", "absent", "unknown"]:
    """Report a CLI login by presence only (unverified), never by reading a secret."""
    environ = config.environ
    home = environ.get("HOME")
    path = credential_path(profile, home=Path(home), env=environ) if home else None
    if path is not None and path.is_file():
        return "present"
    service = _KEYCHAIN_SERVICES.get(profile.name)
    if service is None:
        return "absent"
    found = config.keychain(service)
    return "unknown" if found is None else "present" if found else "absent"


def _provider_auth(name: str, config: HomeConfig, stored: Mapping[str, str | None]) -> ProviderAuth:
    profile = provider_profile(name)
    keys = [_key_var(variable, config.environ, stored) for variable in key_variables(profile)]
    cli_session = _cli_session(profile, config)
    if any(key.source != "missing" for key in keys):
        status = "key"
    elif cli_session == "present":
        status = "cli_session"
    else:
        status = "missing"
    return ProviderAuth(
        provider=name,
        display_name=profile.display_name,
        status=status,
        keys=keys,
        cli_session=cli_session,
        login_command=_LOGIN_COMMANDS.get(name, profile.binary),
    )


def auth_status(request: Request) -> AuthStatus:
    """``GET /api/auth``: where each provider's credentials come from; values never leave."""
    config = request.config
    stored = _stored(config.dotenv_path)
    return AuthStatus(
        providers=[_provider_auth(name, config, stored) for name in SHIPPED_PROVIDERS],
        dotenv_path=str(config.dotenv_path),
    )


def _check_value(value: str) -> None:
    if not value.strip():
        message = "the key is empty"
        raise ApiError(ErrorCode.INVALID_KEY, message)
    if any(unicodedata.category(character) == "Cc" for character in value):
        message = "the key contains a control character"
        raise ApiError(ErrorCode.INVALID_KEY, message)
    if any(character in value for character in "'\"\\$"):
        message = "the key contains a quote, backslash, or `$`"
        raise ApiError(ErrorCode.INVALID_KEY, message)


def store_key(path: Path, name: str, value: str) -> None:
    """Set ``name='value'`` in the `.env` at *path*, keeping every other byte of the file.

    The caller has rejected quotes, backslashes, ``$``, and control characters,
    so python-dotenv neither unescapes nor interpolates the value. Bytes are
    decoded without newline translation, so CRLF files keep their line endings.
    """
    text = path.read_bytes().decode("utf-8") if path.is_file() else ""
    line = f"{name}='{value}'\n"
    kept: list[str] = []
    for binding in parse_stream(io.StringIO(text)):
        if binding.key != name:
            kept.append(binding.original.string)
        elif line not in kept:
            kept.append(line)
    if line not in kept:
        if kept and not kept[-1].endswith("\n"):
            kept.append("\n")
        kept.append(line)
    atomic_write(path, "".join(kept).encode("utf-8"), mode=0o600)


def write_key(request: Request) -> KeyWriteResult:
    """``PUT /api/auth/{provider}``: store one allowlisted key; the value is never returned."""
    provider = request.params[0]
    if provider not in SHIPPED_PROVIDERS:
        message = f"unknown provider {provider!r}"
        raise ApiError(ErrorCode.UNKNOWN_PROVIDER, message)
    body = parse_body(request, KeyWrite)
    allowed = key_variables(provider_profile(provider))
    if body.name not in allowed:
        expected = ", ".join(allowed) or "none; sign in with the provider's CLI"
        message = f"{body.name!r} is not a key variable for {provider} (expected: {expected})"
        raise ApiError(ErrorCode.INVALID_KEY, message)
    value = body.value.get_secret_value()
    _check_value(value)
    config = request.config
    with config.write_lock:
        store_key(config.dotenv_path, body.name, value)
    return KeyWriteResult(
        provider=provider,
        name=body.name,
        status="unverified",
        shadowed_by_env=body.name in config.environ,
    )
```

In `app.py`, import `keys` and add:

```python
    ("GET", re.compile(r"/api/auth"), keys.auth_status),
    ("PUT", re.compile(r"/api/auth/([^/]+)"), keys.write_key),
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_auth.py libs/vs-agent/tests -q --basetemp=/tmp/vsw`
Expected: PASS. On a Mac signed in to Claude Code, `uv run python -c "from entrypoints.web_home.context import keychain_has; print(keychain_has('Claude Code-credentials'))"` prints `True` without a keychain prompt for the secret.

- [ ] **Step 7: Commit**

```bash
git add libs/vs-agent/src/vs_agent src/entrypoints/web_home tests/entrypoints/web_home/test_home_auth.py
git commit -m "feat(web): report provider sign-in state and store keys write-only"
```

### Task 6: Group (a) gates, docs, and PR

**Files:**
- Modify: `docs/contributing/web-development.md` (new section)

- [ ] **Step 1: Document the command**

Append to `docs/contributing/web-development.md`:

```markdown
## Home server

`uv run python -m entrypoints.web home` serves the desktop app and its setup API on
`127.0.0.1:8764` and prints `VibeSys home: <capability URL>`. Pass `--port` once to change
the saved port, `--root DIR` (repeatable) to limit the folder picker (default: your home
directory), and `--dev-origin http://127.0.0.1:5173` when the Vite dev server proxies `/api`.
The API contract is `src/entrypoints/web_home/contract.py`; print its JSON Schema with
`uv run python -m entrypoints.web_home.contract`. Keys saved from the app go to the
checkout's `.env` (mode 0600); the server never loads that file into its own environment.
```

- [ ] **Step 2: Run the gates**

```bash
./scripts/format.sh
./scripts/check_format.sh
./scripts/check_lint.sh
./scripts/check_types.sh
uv run tach check
uv run python scripts/check_lint_waivers.py
uv run python scripts/check_file_length.py
uv run python scripts/check_test_isolation.py
uv run python scripts/check_doc_links.py
uv run pytest tests/entrypoints tests/server/test_chat_manager.py libs/vs-agent/tests libs/vs-project/tests -q --basetemp=/tmp/vsw
uv run python -m entrypoints.web_home.contract > /dev/null
```

Expected: every command exits 0. Fix any finding in place; do not add a waiver without the rationale format above.

- [ ] **Step 3: Commit and open the PR**

```bash
git add docs/contributing/web-development.md
git commit -m "docs(web): document vibesys web home"
```

Open with the `open-pr` skill: title `feat(web): home server with folder, project, catalog, and auth API`, base `main`, body from `.github/pull_request_template.md` (Problem: the app cannot pick a project or save keys without the CLI; Solution: the group (a) endpoints and security rules; Verification: the commands above and the Review Focus tests 1 to 4).

---

# PR group (b): tasks and commit (stacked on group (a))

### Task 7: `Project.create_task`

**Files:**
- Modify: `libs/vs-project/src/vs_project/_layout.py` (new `TaskExistsError`, `ProjectLayout.create_task`), `libs/vs-project/src/vs_project/project.py`, `libs/vs-project/src/vs_project/api/__init__.py`
- Test: `libs/vs-project/tests/test_create_task.py`

**Interfaces:**
- Produces: `Project.create_task(task_name: TaskName | str, *, objective: str, manifest: str) -> TaskDirectory`; `vs_project.api.TaskExistsError`.

- [ ] **Step 1: Write the failing tests**

`libs/vs-project/tests/test_create_task.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_project.api import InvalidTaskNameError, Project, TaskExistsError, UnsafeProjectPathError

if TYPE_CHECKING:
    from pathlib import Path


def test_create_task_makes_the_tasks_root_and_returns_a_discoverable_task(tmp_path: Path) -> None:
    project = Project.open(tmp_path)

    task = project.create_task("serve", objective="Go faster.\n", manifest="version = 1\n")

    assert project.is_initialized()
    assert project.discover_tasks() == (task,)
    assert task.objective_path.read_text() == "Go faster.\n"
    assert task.manifest_path.read_text() == "version = 1\n"


def test_create_task_refuses_an_existing_name(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    project.create_task("serve", objective="a", manifest="b")

    with pytest.raises(TaskExistsError, match="serve"):
        project.create_task("serve", objective="c", manifest="d")
    assert project.select_task("serve").objective_path.read_text() == "a"


@pytest.mark.parametrize("name", ["Bad Name", "../escape", "", ".hidden"])
def test_create_task_rejects_names_that_are_not_one_task_directory(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(InvalidTaskNameError):
        Project.open(tmp_path).create_task(name, objective="a", manifest="b")
    assert not (tmp_path / ".vibesys").exists()


def test_create_task_refuses_a_symlinked_configuration_root(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".vibesys").symlink_to(elsewhere)

    with pytest.raises(UnsafeProjectPathError):
        Project.open(project_root).create_task("serve", objective="a", manifest="b")
    assert list(elsewhere.iterdir()) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest libs/vs-project/tests/test_create_task.py -q --basetemp=/tmp/vsw`
Expected: `ImportError: cannot import name 'TaskExistsError'`.

- [ ] **Step 3: Implement**

In `_layout.py`, add before `class TaskNotFoundError`:

```python
class TaskExistsError(ProjectLayoutError):
    """Raised when a new task would replace an existing task directory."""

    @classmethod
    def existing(cls, task_name: TaskName) -> Self:
        """Describe a task name that is already taken."""
        return cls(f"VibeSys task {task_name.value!r} already exists")
```

and add this method to `ProjectLayout` before `_load_task`:

```python
    def create_task(
        self, task_name: TaskName | str, *, objective: str, manifest: str
    ) -> TaskDirectory:
        """Create one task directory with its objective and manifest files."""
        name = TaskName(task_name) if isinstance(task_name, str) else task_name
        configuration_path = self._configuration_path()
        if configuration_path.is_symlink():
            description = "VibeSys configuration root"
            raise UnsafeProjectPathError.symlink(description, configuration_path)
        (configuration_path / _TASKS_DIRECTORY_NAME).mkdir(parents=True, exist_ok=True)
        tasks_root = self.tasks_root()
        lexical_path = tasks_root.path / name.value
        try:
            lexical_path.mkdir()
        except FileExistsError as exc:
            raise TaskExistsError.existing(name) from exc
        (lexical_path / _OBJECTIVE_FILE_NAME).write_text(objective, encoding="utf-8")
        (lexical_path / _MANIFEST_FILE_NAME).write_text(manifest, encoding="utf-8")
        return self._load_task(tasks_root, name, lexical_path)
```

In `project.py`, add before `is_state_initialized`:

```python
    def create_task(
        self, task_name: TaskName | str, *, objective: str, manifest: str
    ) -> TaskDirectory:
        """Create a task under the tasks root, creating the root on first use.

        Raises ``TaskExistsError`` when the name is taken and
        ``InvalidTaskNameError`` when it is not a valid task name.
        """
        return self._layout.create_task(task_name, objective=objective, manifest=manifest)
```

Export `TaskExistsError` from `vs_project/api/__init__.py` (import list and `__all__`, after `TaskDirectory`).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest libs/vs-project/tests -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add libs/vs-project
git commit -m "feat(vs-project): create a repository-native task through Project"
```

### Task 8: Task list and detail

**Files:**
- Modify: `src/entrypoints/web_home/contract.py` (group (b) models), `src/vibesys/api/request.py` (re-exports), `src/entrypoints/web_home/app.py`
- Create: `src/entrypoints/web_home/tasks.py`
- Test: `tests/entrypoints/web_home/test_home_tasks.py`

**Interfaces:**
- Consumes: `resolve_project`, `load_project_task`, `render_input_manifest`, `InputManifest`.
- Produces: `tasks.project_of(request) -> Project`; `tasks.select_task(project, name) -> TaskDirectory` (typed `unknown_task`/`task_invalid`); `tasks.content_hash(task) -> str`; `tasks.read_only_reason(manifest) -> str | None`; `tasks.detail(project, task) -> TaskDetail`.

- [ ] **Step 1: Write the failing tests**

`tests/entrypoints/web_home/test_home_tasks.py` (Tasks 9 and 10 append to it):

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from tests.entrypoints.web_home.support import make_project, project_key

if TYPE_CHECKING:
    from pathlib import Path

    from tests.entrypoints.web_home.support import Home
```

```python
def _setup(
    home: Home, *, tasks: tuple[str, ...] = ("bench",), commit: bool = True
) -> tuple[str, Path]:
    root = make_project(home.workspace / "proj", tasks=tasks, commit=commit)
    return project_key(home, root), root


def test_tasks_list_and_detail_expose_the_manifest(home: Home) -> None:
    key, _ = _setup(home)

    listing = home.get(f"/api/projects/{key}/tasks").json()
    detail = home.get(f"/api/projects/{key}/tasks/bench").json()

    assert listing == {
        "tasks": [{"name": "bench", "valid": True, "domain": "generic", "error": None}]
    }
    assert detail["objective"] == "Make bench faster.\n"
    assert detail["benchmark_command"] == "python bench.py"
    assert detail["result"] == {
        "kind": "metric",
        "json_argument": "--json",
        "metric": "throughput",
        "protocol_version": None,
    }
    assert (detail["editable"], detail["read_only_reason"]) == (True, None)
    assert len(detail["content_hash"]) == 64


def test_unknown_project_and_task_are_typed_errors(home: Home) -> None:
    key, _ = _setup(home)

    assert home.get("/api/projects/0000/tasks").json()["error"]["code"] == "unknown_project"
    assert home.get(f"/api/projects/{key}/tasks/nope").json()["error"]["code"] == "unknown_task"


def _make_protocol_task(root: Path) -> None:
    manifest = root / ".vibesys" / "tasks" / "bench" / "vibesys.input.toml"
    text = manifest.read_text().split("[benchmark.result]")[0]
    manifest.write_text(
        text.replace(
            'command = ["python", "bench.py"]',
            'command = ["python", "bench.py"]\nresult_protocol = 2',
        )
    )


def test_protocol_tasks_are_read_only(home: Home) -> None:
    key, root = _setup(home)
    _make_protocol_task(root)

    detail = home.get(f"/api/projects/{key}/tasks/bench").json()

    assert detail["result"] == {
        "kind": "protocol",
        "json_argument": None,
        "metric": None,
        "protocol_version": 2,
    }
    assert detail["editable"] is False
    assert "[benchmark.result]" in detail["read_only_reason"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_tasks.py -q --basetemp=/tmp/vsw`
Expected: FAIL, `not_found`.

- [ ] **Step 3: Add the group (b) contract**

In `contract.py`, change `from vibesys.api import ComputeBackend` to `from vibesys.api import ComputeBackend, DomainName`, insert before `SCHEMA_MODELS`:

```python
# Group (b): tasks and commit.


class TaskSummary(_Model):
    """One task in ``GET .../tasks``; ``error`` is set when it does not load."""

    name: str
    valid: bool
    domain: str | None = None
    error: str | None = None


class TaskList(_Model):
    """Response of ``GET /api/projects/{id}/tasks``."""

    tasks: list[TaskSummary]


class ResultContract(_Model):
    """How the benchmark reports its score."""

    kind: Literal["metric", "protocol", "none"]
    json_argument: str | None = None
    metric: str | None = None
    protocol_version: int | None = None


class TaskDetail(_Model):
    """Response of task detail, create, and edit."""

    name: str
    objective: str
    domain: DomainName
    accuracy_command: str
    benchmark_command: str
    result: ResultContract
    profile_guided: bool
    editable: bool
    read_only_reason: str | None
    content_hash: str


class TaskForm(_Model):
    """The editable task fields; commands are shell-quoted strings split with ``shlex``."""

    objective: str = Field(min_length=1)
    domain: DomainName
    accuracy_command: str
    benchmark_command: str
    result_json_argument: str
    result_metric: str


class TaskCreate(TaskForm):
    """Body of ``POST .../tasks``."""

    name: str


class TaskEdit(TaskForm):
    """Body of ``PUT .../tasks/{name}``; ``base_hash`` is the detail's ``content_hash``."""

    base_hash: str


class CommitPreview(_Model):
    """Pending changes split into committable task files and everything else."""

    task_files: list[str]
    other: list[str]


class CommitRequest(_Model):
    """Body of ``POST .../commit``: exactly the previewed ``task_files``."""

    paths: list[str]
    message: str | None = None


class CommitResult(_Model):
    """Response of ``POST .../commit``."""

    commit: str
    committed: list[str]
```

and append to the `SCHEMA_MODELS` tuple: `TaskList, TaskDetail, TaskCreate, TaskEdit, CommitPreview, CommitRequest, CommitResult,`.

In `src/vibesys/api/request.py`, add `InputManifest,` and `render_input_manifest,` to the `from vibesys.inputs import (...)` list and `"InputManifest",` / `"render_input_manifest",` to `__all__` (sorted).

- [ ] **Step 4: Implement list and detail**

`src/entrypoints/web_home/tasks.py`:

```python
"""Task list, detail, create, and edit over `.vibesys/tasks`, and committing task files."""

from __future__ import annotations

import hashlib
import tomllib
from typing import TYPE_CHECKING

from entrypoints.web_home.contract import (
    ApiError,
    ErrorCode,
    ResultContract,
    TaskDetail,
    TaskList,
    TaskSummary,
)
from entrypoints.web_home.projects import resolve_project
from vibesys.api.request import InputManifest, load_project_task, render_input_manifest
from vs_project.api import Project, ProjectError, TaskNotFoundError

if TYPE_CHECKING:
    from vs_project.api import TaskDirectory

    from entrypoints.web_home.context import Request


def project_of(request: Request) -> Project:
    """Open the project named by the first path parameter."""
    return Project.open(resolve_project(request.config, request.params[0]))


def select_task(project: Project, name: str) -> TaskDirectory:
    """Return one task by name with typed errors."""
    try:
        return project.select_task(name)
    except TaskNotFoundError:
        message = f"no task named {name!r}"
        raise ApiError(ErrorCode.UNKNOWN_TASK, message) from None
    except (ProjectError, ValueError) as error:
        raise ApiError(ErrorCode.TASK_INVALID, str(error)) from None


def content_hash(task: TaskDirectory) -> str:
    """Hash both task files; an edit must name the hash it was based on."""
    digest = hashlib.sha256(task.objective_path.read_bytes())
    digest.update(b"\0")
    digest.update(task.manifest_path.read_bytes())
    return digest.hexdigest()


def read_only_reason(manifest: InputManifest) -> str | None:
    """Return why the form cannot edit *manifest* without losing information, or ``None``."""
    if manifest.accuracy.command is None or manifest.benchmark.command is None:
        return "evaluator entrypoint commands are edited in vibesys.input.toml"
    if manifest.benchmark.result is None:
        return "only tasks with a [benchmark.result] metric are editable here"
    rendered = InputManifest.model_validate(tomllib.loads(render_input_manifest(manifest)))
    if rendered != manifest:
        return "the manifest has settings this editor would drop; edit vibesys.input.toml"
    return None


def _result(manifest: InputManifest) -> ResultContract:
    benchmark = manifest.benchmark
    if benchmark.result is not None:
        return ResultContract(
            kind="metric",
            json_argument=benchmark.result.json_argument,
            metric=benchmark.result.metric,
        )
    if benchmark.result_protocol is not None:
        return ResultContract(kind="protocol", protocol_version=benchmark.result_protocol)
    return ResultContract(kind="none")


def detail(project: Project, task: TaskDirectory) -> TaskDetail:
    """Load one task into its API shape, or raise ``task_invalid`` with the loader's message."""
    try:
        bundle = load_project_task(project, task)
    except (OSError, ValueError, ProjectError) as error:
        raise ApiError(ErrorCode.TASK_INVALID, str(error)) from None
    manifest = bundle.manifest
    reason = read_only_reason(manifest)
    return TaskDetail(
        name=task.name.value,
        objective=bundle.objective,
        domain=manifest.agent.domain,
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        result=_result(manifest),
        profile_guided=manifest.profile_guided is not None,
        editable=reason is None,
        read_only_reason=reason,
        content_hash=content_hash(task),
    )


def task_list(request: Request) -> TaskList:
    """``GET /api/projects/{id}/tasks``."""
    project = project_of(request)
    try:
        tasks = project.discover_tasks() if project.is_initialized() else ()
    except ProjectError as error:
        raise ApiError(ErrorCode.TASK_INVALID, str(error)) from None
    summaries: list[TaskSummary] = []
    for task in tasks:
        try:
            summaries.append(
                TaskSummary(name=task.name.value, valid=True, domain=detail(project, task).domain)
            )
        except ApiError as error:
            summaries.append(TaskSummary(name=task.name.value, valid=False, error=error.message))
    return TaskList(tasks=summaries)


def task_detail(request: Request) -> TaskDetail:
    """``GET /api/projects/{id}/tasks/{name}``."""
    project = project_of(request)
    return detail(project, select_task(project, request.params[1]))
```

In `app.py`, import `tasks`, add `_PROJECT = r"/api/projects/([^/]+)"` after `_STATE_CHANGING`, and add the routes:

```python
    ("GET", re.compile(_PROJECT + r"/tasks"), tasks.task_list),
    ("GET", re.compile(_PROJECT + r"/tasks/([^/]+)"), tasks.task_detail),
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_tasks.py -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/entrypoints/web_home src/vibesys/api/request.py tests/entrypoints/web_home/test_home_tasks.py
git commit -m "feat(web): list and describe project tasks with an editability check"
```

### Task 9: Create and edit tasks

**Files:**
- Modify: `src/entrypoints/web_home/tasks.py`, `src/entrypoints/web_home/app.py`
- Test: `tests/entrypoints/web_home/test_home_tasks.py`

**Interfaces:**
- Produces: `tasks.build_manifest(form: TaskForm, base: InputManifest | None) -> InputManifest`; `create_task`, `edit_task` handlers.

- [ ] **Step 1: Append the failing tests**

Add `import tomllib` and `import pytest` to the test module's imports, then append:

```python
FORM = {
    "objective": "Raise throughput.\n",
    "domain": "generic",
    "accuracy_command": "python check.py --strict",
    "benchmark_command": "python 'bench suite.py'",
    "result_json_argument": "--json",
    "result_metric": "tokens_per_s",
}


def test_create_writes_both_files_and_the_task_loads(home: Home) -> None:
    key, root = _setup(home, tasks=())

    created = home.post(f"/api/projects/{key}/tasks", {**FORM, "name": "serve"}).json()

    assert created["benchmark_command"] == "python 'bench suite.py'"
    assert (root / ".vibesys" / "tasks" / "serve" / "OBJECTIVE.md").read_text() == FORM["objective"]
    manifest = tomllib.loads(
        (root / ".vibesys" / "tasks" / "serve" / "vibesys.input.toml").read_text()
    )
    assert manifest["benchmark"]["result"] == {"json_argument": "--json", "metric": "tokens_per_s"}
    again = home.post(f"/api/projects/{key}/tasks", {**FORM, "name": "serve"}).json()
    assert again["error"]["code"] == "task_exists"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"name": "Bad Name"}, "task_invalid"),
        ({"name": "ok", "benchmark_command": "   "}, "task_invalid"),
        ({"name": "ok", "accuracy_command": "python 'unterminated"}, "task_invalid"),
        ({"name": "ok", "result_json_argument": "json"}, "task_invalid"),
        ({"name": "ok", "domain": "astrology"}, "invalid_request"),
    ],
)
def test_create_rejects_invalid_forms(home: Home, change: dict[str, str], code: str) -> None:
    key, _ = _setup(home, tasks=())

    reply = home.post(f"/api/projects/{key}/tasks", {**FORM, **change})

    assert reply.json()["error"]["code"] == code


def test_edit_needs_the_current_hash(home: Home) -> None:
    key, root = _setup(home)
    base = home.get(f"/api/projects/{key}/tasks/bench").json()["content_hash"]

    edited = home.put(f"/api/projects/{key}/tasks/bench", {**FORM, "base_hash": base}).json()
    stale = home.put(f"/api/projects/{key}/tasks/bench", {**FORM, "base_hash": base}).json()

    assert edited["objective"] == FORM["objective"]
    assert edited["content_hash"] != base
    assert stale["error"]["code"] == "task_conflict"
    assert (root / ".vibesys" / "tasks" / "bench" / "OBJECTIVE.md").read_text() == FORM["objective"]


def test_edit_keeps_manifest_settings_the_form_does_not_show(home: Home) -> None:
    key, root = _setup(home)
    manifest = root / ".vibesys" / "tasks" / "bench" / "vibesys.input.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'command = ["python", "bench.py"]',
            'command = ["python", "bench.py"]\ntimeout_seconds = 90',
        )
    )
    base = home.get(f"/api/projects/{key}/tasks/bench").json()["content_hash"]

    home.put(f"/api/projects/{key}/tasks/bench", {**FORM, "base_hash": base})

    assert tomllib.loads(manifest.read_text())["benchmark"]["timeout_seconds"] == 90


def test_editing_a_read_only_task_is_refused(home: Home) -> None:
    key, root = _setup(home)
    _make_protocol_task(root)
    base = home.get(f"/api/projects/{key}/tasks/bench").json()["content_hash"]

    reply = home.put(f"/api/projects/{key}/tasks/bench", {**FORM, "base_hash": base})

    assert reply.json()["error"]["code"] == "task_read_only"
```

(Place `FORM` right after the imports.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_tasks.py -q --basetemp=/tmp/vsw`
Expected: the new tests FAIL with `not_found`.

- [ ] **Step 3: Implement**

In `tasks.py` add `import shlex` and `from typing import TYPE_CHECKING, Any`; add `from pydantic import ValidationError`; import `atomic_write`, `parse_body`, `validation_errors` from `entrypoints.web_home.context`; add `TaskCreate`, `TaskEdit`, `TaskForm` to the contract import; add `TaskExistsError` to the `vs_project.api` import. Append:

```python
def _argv(raw: str, field: str) -> list[str]:
    try:
        parts = shlex.split(raw)
    except ValueError as error:
        message = f"{field}: {error}"
        raise ApiError(ErrorCode.TASK_INVALID, message) from None
    if not parts:
        message = f"{field} must contain a command"
        raise ApiError(ErrorCode.TASK_INVALID, message)
    return parts


def build_manifest(form: TaskForm, base: InputManifest | None) -> InputManifest:
    """Apply the form to *base* (or a new scalar-metric task) and validate the result."""
    data: dict[str, Any] = (
        base.model_dump(mode="json", exclude_none=True) if base is not None else {"version": 1}
    )
    data["agent"] = {"domain": form.domain.value}
    data["accuracy"] = {
        **data.get("accuracy", {}),
        "command": _argv(form.accuracy_command, "accuracy_command"),
    }
    data["benchmark"] = {
        **data.get("benchmark", {}),
        "command": _argv(form.benchmark_command, "benchmark_command"),
        "result": {"json_argument": form.result_json_argument, "metric": form.result_metric},
    }
    try:
        return InputManifest.model_validate(data)
    except ValidationError as error:
        message = "the task manifest is invalid"
        raise ApiError(
            ErrorCode.TASK_INVALID, message, details={"errors": validation_errors(error)}
        ) from None


def create_task(request: Request) -> TaskDetail:
    """``POST /api/projects/{id}/tasks``: write OBJECTIVE.md and vibesys.input.toml."""
    body = parse_body(request, TaskCreate)
    project = project_of(request)
    manifest = render_input_manifest(build_manifest(body, None))
    with request.config.write_lock:
        try:
            task = project.create_task(body.name, objective=body.objective, manifest=manifest)
        except TaskExistsError as error:
            raise ApiError(ErrorCode.TASK_EXISTS, str(error)) from None
        except (ProjectError, ValueError) as error:
            raise ApiError(ErrorCode.TASK_INVALID, str(error)) from None
    return detail(project, task)


def edit_task(request: Request) -> TaskDetail:
    """``PUT /api/projects/{id}/tasks/{name}``: replace both files if nothing changed since."""
    body = parse_body(request, TaskEdit)
    project = project_of(request)
    with request.config.write_lock:
        task = select_task(project, request.params[1])
        current = detail(project, task)
        if current.content_hash != body.base_hash:
            message = "the task changed on disk since it was loaded; reload it"
            raise ApiError(ErrorCode.TASK_CONFLICT, message)
        if current.read_only_reason is not None:
            raise ApiError(ErrorCode.TASK_READ_ONLY, current.read_only_reason)
        manifest = build_manifest(body, load_project_task(project, task).manifest)
        atomic_write(task.objective_path, body.objective.encode("utf-8"), mode=0o644)
        atomic_write(
            task.manifest_path, render_input_manifest(manifest).encode("utf-8"), mode=0o644
        )
    return detail(project, task)
```

Routes in `app.py`:

```python
    ("POST", re.compile(_PROJECT + r"/tasks"), tasks.create_task),
    ("PUT", re.compile(_PROJECT + r"/tasks/([^/]+)"), tasks.edit_task),
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_tasks.py -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/entrypoints/web_home tests/entrypoints/web_home/test_home_tasks.py
git commit -m "feat(web): create and edit tasks with hash-based conflict detection"
```

### Task 10: Commit task files

**Files:**
- Modify: `src/entrypoints/web_home/tasks.py`, `src/entrypoints/web_home/app.py`
- Test: `tests/entrypoints/web_home/test_home_tasks.py`

- [ ] **Step 1: Append the failing tests**

The first test also pins the regression that a changed `.vibesys/web-gateway.json` (which holds a capability token) is never committable. Add `from tests.support import run_test_command` to the imports, then append:

```python
def test_commit_previews_then_commits_only_task_files(home: Home) -> None:
    key, root = _setup(home, tasks=())
    home.post(f"/api/projects/{key}/tasks", {**FORM, "name": "serve"})
    (root / "notes.txt").write_text("mine")
    (root / ".vibesys" / "web-gateway.json").write_text('{"token": "secret"}')

    preview = home.get(f"/api/projects/{key}/commit").json()
    stale = home.post(f"/api/projects/{key}/commit", {"paths": preview["task_files"][:1]}).json()
    result = home.post(f"/api/projects/{key}/commit", {"paths": preview["task_files"]}).json()

    assert preview == {
        "task_files": [
            ".vibesys/tasks/serve/OBJECTIVE.md",
            ".vibesys/tasks/serve/vibesys.input.toml",
        ],
        "other": [".vibesys/web-gateway.json", "notes.txt"],
    }
    assert stale["error"]["code"] == "task_conflict"
    assert result["committed"] == preview["task_files"]
    status = run_test_command(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    assert status.stdout == "?? .vibesys/web-gateway.json\n?? notes.txt\n"


def test_commit_works_in_a_repository_without_commits(home: Home) -> None:
    key, root = _setup(home, commit=False)
    (root / "README.md").unlink()

    preview = home.get(f"/api/projects/{key}/commit").json()
    result = home.post(f"/api/projects/{key}/commit", {"paths": preview["task_files"]})

    assert result.status == 200
    assert home.post("/api/projects/validate", {"path": str(root)}).json()["state"] == "ready"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_tasks.py -q --basetemp=/tmp/vsw`
Expected: the two new tests FAIL with `not_found`.

- [ ] **Step 3: Implement**

In `tasks.py`, add `git` and `pending_changes` to the `entrypoints.web_home.context` import and `CommitPreview`, `CommitRequest`, `CommitResult` to the contract import; add `_COMMIT_TAIL_LINES = 20` after the imports and append:

```python
def _preview(project: Project) -> CommitPreview:
    """Split pending changes; only files under the tasks root are ever committable.

    Other `.vibesys/` content (discovery records with capability tokens, state)
    is never offered.
    """
    pending = pending_changes(project.root)
    try:
        prefix = project.tasks_root().path.relative_to(project.root).as_posix() + "/"
    except ProjectError:
        return CommitPreview(task_files=[], other=pending)
    return CommitPreview(
        task_files=[path for path in pending if path.startswith(prefix)],
        other=[path for path in pending if not path.startswith(prefix)],
    )


def commit_preview(request: Request) -> CommitPreview:
    """``GET /api/projects/{id}/commit``: what a commit would include."""
    return _preview(project_of(request))


def _tail(text: str) -> list[str]:
    return text.splitlines()[-_COMMIT_TAIL_LINES:]


def commit(request: Request) -> CommitResult:
    """``POST /api/projects/{id}/commit``: commit exactly the previewed task files."""
    body = parse_body(request, CommitRequest)
    project = project_of(request)
    with request.config.write_lock:
        preview = _preview(project)
        if not preview.task_files or sorted(body.paths) != preview.task_files:
            message = "the task files changed since the preview; review them again"
            raise ApiError(
                ErrorCode.TASK_CONFLICT, message, details={"task_files": list(preview.task_files)}
            )
        for arguments in (
            ("add", "--", *preview.task_files),
            (
                "commit",
                "--quiet",
                "-m",
                body.message or "vibesys: add task files",
                "--",
                *preview.task_files,
            ),
        ):
            result = git(project.root, *arguments)
            if result.returncode != 0:
                message = f"git {arguments[0]} failed"
                raise ApiError(
                    ErrorCode.COMMIT_FAILED, message, details={"stderr_tail": _tail(result.stderr)}
                )
        head = git(project.root, "rev-parse", "HEAD").stdout.strip()
    return CommitResult(commit=head, committed=preview.task_files)
```

Routes in `app.py`:

```python
    ("GET", re.compile(_PROJECT + r"/commit"), tasks.commit_preview),
    ("POST", re.compile(_PROJECT + r"/commit"), tasks.commit),
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_tasks.py -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/entrypoints/web_home tests/entrypoints/web_home/test_home_tasks.py
git commit -m "feat(web): preview and commit task-file changes"
```

### Task 11: Group (b) gates and PR

- [ ] **Step 1: Run the gates**

```bash
./scripts/format.sh
./scripts/check_format.sh
./scripts/check_lint.sh
./scripts/check_types.sh
uv run tach check
uv run python scripts/check_lint_waivers.py
uv run python scripts/check_file_length.py
uv run python scripts/check_test_isolation.py
uv run pytest tests/entrypoints libs/vs-project/tests tests/vibesys/test_input_sources.py -q --basetemp=/tmp/vsw
uv run python -m entrypoints.web_home.contract > /dev/null
```

Expected: every command exits 0.

- [ ] **Step 2: Open the stacked PR**

`open-pr` skill, title `feat(web): task authoring and commit API`, base: the group (a) branch (native GitHub stack). CI runs only for `main`-based PRs; "no checks" on this PR is expected until group (a) merges.

---

# PR group (c): runs and notes (stacked on group (b); needs sub-project 1)

### Task 12: Start a run

**Files:**
- Modify: `src/entrypoints/web_home/contract.py` (group (c) models), `src/entrypoints/web_home/context.py`, `src/entrypoints/web_home/app.py`
- Create: `src/entrypoints/web_home/runs.py`, `tests/entrypoints/web_home/fake_run_server.py`
- Test: `tests/entrypoints/web_home/test_home_runs.py`

**Interfaces:**
- Consumes: `inspect_project`, `project_id`, `resolve_project`, `tasks.select_task`, `catalog.LOOPS`, `WebInstanceRecord` with `run_id`/`mode` (sub-project 1).
- Produces: `runs.Owner(run_id, pid, origin)`, the atomically written `<record>.owner.json` sidecar that ties a home-owned record to one launch attempt; `runs._owner_run(path, record, project, *, external) -> str | None` (ours: the sidecar's run when its pid equals the record's; external: the current run); `runs._reap(process)`; `HomeConfig.launch_timeout` (tests shorten it); `HomeConfig.run_server_argv` (default `(sys.executable, "-m", "entrypoints.server")`, tests inject the fake), `HomeConfig.launch_lock`, `HomeConfig.launches: dict[Path, Launch]`; `safe_segment(value) -> str`; `runs.Launch(run_id, process, stderr_log)`; `runs.render_run_config(StartRun) -> str`; `runs._spawn(config, root, record_path, run_id, arguments) -> WebInstanceRecord`; `runs.stderr_tail(log) -> list[str]`.

- [ ] **Step 1: Write the fake run server**

`tests/entrypoints/web_home/fake_run_server.py` stands in for `entrypoints.server`, so the tests exercise spawn, discovery, stderr capture, timeouts, SIGTERM, and the gateway's token and Origin checks without a real run (Task 13 adds one test against the real run server):

```python
"""A stand-in for `python -m entrypoints.server --web --detach` used by the runs tests.

It publishes a discovery record shaped like sub-project 1's (`mode`, and
`run_id` only for `--web-reopen-run`), answers `/health?token=`, and checks
`/ws` like the real gateway (`websocket.py`): the capability token, then an
exact `Origin` from `--web-origin` or its own origin. It records its argv next
to the record and removes the record on SIGTERM.

`FAKE_RUN_SERVER_FAIL=1` writes to stderr and exits 2; `FAKE_RUN_SERVER_HANG=1`
never publishes a record.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from types import FrameType


def _option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


class _Gateway(BaseHTTPRequestHandler):
    token: ClassVar[str] = ""
    origins: ClassVar[frozenset[str]] = frozenset()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        token = parse_qs(parsed.query).get("token", [""])[0]
        if not secrets.compare_digest(token, self.token):
            self._reply(403, b"Invalid VibeSys capability token\n")
        elif parsed.path == "/health":
            self._reply(200, b"vibesys-ok\n")
        elif parsed.path == "/ws" and self.headers.get("Origin") not in self.origins:
            self._reply(403, b"Invalid WebSocket origin\n")
        elif parsed.path == "/ws":
            self._reply(200, b"origin-ok\n")
        else:
            self._reply(404, b"Not found\n")

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        del format, args


def main() -> None:
    argv = sys.argv[1:]
    if os.environ.get("FAKE_RUN_SERVER_FAIL") == "1":
        sys.stderr.write("Traceback: boom\nConfigurationError: bad run\n")
        raise SystemExit(2)
    if os.environ.get("FAKE_RUN_SERVER_HANG") == "1":
        signal.pause()
    record_path = Path(_option(argv, "--web-instance") or "")
    reopen = _option(argv, "--web-reopen-run")
    server = HTTPServer(("127.0.0.1", 0), _Gateway)
    port = server.server_address[1]
    _Gateway.token = secrets.token_urlsafe(16)
    allowed = [argv[i + 1] for i, item in enumerate(argv) if item == "--web-origin"]
    _Gateway.origins = frozenset({f"http://127.0.0.1:{port}", *allowed})
    record_path.with_suffix(".argv.json").write_text(json.dumps(argv))
    record = {
        "version": 1,
        "pid": os.getpid(),
        "port": port,
        "token": _Gateway.token,
        "url": f"http://127.0.0.1:{port}/?token={_Gateway.token}",
        "project_root": str(Path.cwd()),
        "started_at": time.time(),
        "run_id": reopen,
        "mode": "reopen" if reopen else "live",
    }
    temporary = record_path.with_name(record_path.name + ".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(record_path)

    def stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        record_path.unlink(missing_ok=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the failing tests**

`tests/entrypoints/web_home/test_home_runs.py` (Tasks 13 and 14 append to it):

```python
from __future__ import annotations

import json
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from entrypoints.web_home.contract import StartRun
from entrypoints.web_home.runs import render_run_config
from tests.entrypoints.web_home.support import make_project, project_key
from vibesys.api import Config

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.entrypoints.web_home.support import Home
```

```python
FAKE = Path(__file__).with_name("fake_run_server.py")
START = {
    "task": "bench",
    "outer_loop": "plain",
    "budget": 4,
    "compute_backend": "cpu",
    "provider": "codex",
    "model": "gpt-5.5",
    "roles": {"implementer": {"model": "gpt-5.6-sol", "reasoning_effort": "high"}},
}


@pytest.fixture
def runs_home(home: Home) -> Iterator[Home]:
    home.config.run_server_argv = (sys.executable, str(FAKE))
    try:
        yield home
    finally:
        for launch in home.config.launches.values():
            launch.process.terminate()
            launch.process.wait(timeout=10)


def _project(home: Home) -> tuple[str, Path]:
    root = make_project(home.workspace / "proj")
    return project_key(home, root), root


def _argv(home: Home, name: str = "live") -> list[str]:
    gateways = home.config.state_home / "web" / "gateways"
    return json.loads(next(gateways.glob(f"*/{name}.argv.json")).read_text())


def test_start_returns_the_gateway_and_passes_the_launch_contract(runs_home: Home) -> None:
    key, root = _project(runs_home)

    reply = runs_home.post(f"/api/projects/{key}/runs", START).json()

    argv = _argv(runs_home)
    assert reply["gateway"]["state"] == "starting"
    assert reply["gateway"]["websocket_url"].startswith("ws://127.0.0.1:")
    assert argv[:3] == ["--web", "--detach", "--web-port"]
    assert ["--web-origin", runs_home.config.origin] == argv[argv.index("--web-origin") :][:2]
    assert "http://127.0.0.1:5173" in argv
    for flag, value in (
        ("--project", str(root)),
        ("--task", "bench"),
        ("--outer-loop", "plain"),
        ("--max-rounds", "4"),
        ("--backend", "cpu"),
        ("--cli-provider", "codex"),
        ("--exp-name", reply["run_id"]),
    ):
        assert argv[argv.index(flag) + 1] == value
    config = tomllib.loads(Path(argv[argv.index("--config") + 1]).read_text())
    assert config["agent"]["roles"]["implementer"] == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
    }


def test_start_refuses_a_second_live_run_even_when_racing(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    statuses: list[int] = []

    def start() -> None:
        statuses.append(runs_home.post(f"/api/projects/{key}/runs", START).status)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409]


def test_a_failed_launch_returns_the_stderr_tail(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    runs_home.config.environ = {**runs_home.config.environ, "FAKE_RUN_SERVER_FAIL": "1"}

    reply = runs_home.post(f"/api/projects/{key}/runs", START)

    body = reply.json()
    assert (reply.status, body["error"]["code"]) == (502, "launch_failed")
    assert body["error"]["details"]["stderr_tail"][-1] == "ConfigurationError: bad run"
    log = Path(body["error"]["details"]["stderr_log"])
    assert log.name == "live.stderr.log"
    assert "bad run" in log.read_text()


def test_a_timed_out_launch_is_reaped_and_the_retry_owns_the_gateway(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    runs_home.config.launch_timeout = 1.0
    plain = runs_home.config.environ
    runs_home.config.environ = {**plain, "FAKE_RUN_SERVER_HANG": "1"}

    first = runs_home.post(f"/api/projects/{key}/runs", START).json()
    hung = next(iter(runs_home.config.launches.values())).process
    runs_home.config.environ = plain
    runs_home.config.launch_timeout = 30.0
    second = runs_home.post(f"/api/projects/{key}/runs", START).json()

    assert first["error"]["code"] == "launch_failed"
    assert hung.poll() is not None
    gateways = runs_home.config.state_home / "web" / "gateways"
    owner = json.loads(next(gateways.glob("*/live.owner.json")).read_text())
    record = json.loads(next(gateways.glob("*/live.json")).read_text())
    assert (owner["run_id"], owner["pid"]) == (second["run_id"], record["pid"])
    assert _state(runs_home, key, second["run_id"]) == ("active", "starting")


def test_the_gateway_accepts_the_app_origins_only(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    gateway = runs_home.post(f"/api/projects/{key}/runs", START).json()["gateway"]
    probe = gateway["websocket_url"].replace("ws://", "http://", 1)

    def status(origin: str) -> int:
        request = urllib.request.Request(probe, headers={"Origin": origin})  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    assert status(runs_home.config.origin) == 200
    assert status("http://127.0.0.1:5173") == 200
    assert status("http://evil.test") == 403


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"outer_loop": "loop-de-loop"}, "invalid_request"),
        ({"provider": "copilot"}, "unknown_provider"),
        ({"task": "nope"}, "unknown_task"),
        ({"outer_loop": "profile-guided"}, "profile_guided_unavailable"),
        ({"roles": {"implementer": {"model": ""}}}, "invalid_request"),
        ({"budget": 0}, "invalid_request"),
    ],
)
def test_start_rejects_bad_requests_before_spawning(
    runs_home: Home, change: dict[str, object], code: str
) -> None:
    key, _ = _project(runs_home)

    reply = runs_home.post(f"/api/projects/{key}/runs", {**START, **change}).json()

    assert reply["error"]["code"] == code
    assert runs_home.config.launches == {}


def test_start_refuses_a_dirty_project(runs_home: Home) -> None:
    key, root = _project(runs_home)
    (root / "scratch.txt").write_text("x")

    reply = runs_home.post(f"/api/projects/{key}/runs", START).json()

    assert reply["error"]["code"] == "dirty_tree"
    assert reply["error"]["details"]["pending"] == ["scratch.txt"]


def test_run_config_is_a_valid_agent_config() -> None:
    body = StartRun.model_validate({**START, "driver": "agentshim", "reasoning_effort": "low"})

    config = Config.model_validate(tomllib.loads(render_run_config(body)))

    assert (config.model.name, config.agent.driver, config.agent.cli_provider) == (
        "gpt-5.5",
        "agentshim",
        "codex",
    )
    assert config.thinking.level == "low"
    assert config.agent.roles["implementer"].reasoning_effort == "high"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_runs.py -q --basetemp=/tmp/vsw`
Expected: collection error, `No module named 'entrypoints.web_home.runs'`.

- [ ] **Step 4: Add the group (c) contract**

In `contract.py`, change the imports to `from pydantic.alias_generators import to_camel` (new) and `from vibesys.api import ComputeBackend, DomainName, RunStatus`; insert before `SCHEMA_MODELS`:

```python
# Group (c): runs and notes.


class GatewayState(StrEnum):
    """What the app can do with a run's gateway."""

    LIVE = "live"
    STARTING = "starting"
    ENDED_SERVING = "ended_serving"
    FAILED = "failed"
    STALE = "stale"
    EXTERNAL = "external"
    REOPENED = "reopened"
    NONE = "none"


class Gateway(_Model):
    """A run gateway; connection fields are set only when it answers health probes."""

    state: GatewayState
    url: str | None = None
    websocket_url: str | None = None
    token: str | None = None
    stderr_tail: list[str] = Field(default_factory=list)
    stderr_log: str | None = None
    origin_mismatch: bool = False


class RunRow(_Model):
    """One run, newest first; ``reopen`` is its read-only gateway when one is serving."""

    run_id: str
    loop: str | None
    status: RunStatus
    rounds: int
    gateway: Gateway
    reopen: Gateway | None = None
    error: str | None = None
    task: str | None = None
    objective: str | None = None
    created_at: str | None = None


class RunList(_Model):
    """Response of ``GET /api/projects/{id}/runs``."""

    runs: list[RunRow]


class RoleOverride(_Model):
    """Per-role model controls written to ``[agent.roles.<id>]``."""

    model: str | None = Field(default=None, min_length=1, max_length=256)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=256)


class StartRun(_Model):
    """Body of ``POST /api/projects/{id}/runs``."""

    task: str
    outer_loop: str
    budget: int | None = Field(default=None, ge=1)
    compute_backend: ComputeBackend
    driver: Driver | None = None
    provider: str
    model: str = Field(min_length=1, max_length=256)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=256)
    roles: dict[str, RoleOverride] = Field(default_factory=dict)


class ResumeRun(_Model):
    """Body of ``POST .../runs/{run}/resume``; ``budget`` may only grow."""

    budget: int | None = Field(default=None, ge=1)


class LaunchResult(_Model):
    """Response of start, resume, and open."""

    run_id: str
    gateway: Gateway


class StopResult(_Model):
    """Response of ``DELETE /api/projects/{id}/live``."""

    stopped: bool
    run_id: str | None


class NoteRecord(BaseModel):
    """A TUI-compatible note file (camelCase on the wire and on disk)."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, alias_generator=to_camel, populate_by_name=True
    )

    run_id: str
    text: str
    created_at: str
    updated_at: str


class NoteResponse(_Model):
    """Response of ``GET``/``PUT /api/notes/{run}``."""

    note: NoteRecord | None


class NoteUpdate(_Model):
    """Body of ``PUT /api/notes/{run}``."""

    text: str
```

and append to `SCHEMA_MODELS`: `RunList, StartRun, ResumeRun, LaunchResult, StopResult, NoteResponse, NoteUpdate,`.

- [ ] **Step 5: Extend the context**

In `context.py` add `import re`; under `TYPE_CHECKING` add `from entrypoints.web_home.runs import Launch`; add the constants

```python
_UNSAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9_.-]")
_BMP_LAST = 0xFFFF
```

add these `HomeConfig` fields (the first after `environ`, the others after `write_lock`):

```python
    run_server_argv: tuple[str, ...] = (sys.executable, "-m", "entrypoints.server")
    launch_lock: threading.Lock = field(default_factory=threading.Lock)
    launch_timeout: float = 30.0
    launches: dict[Path, Launch] = field(default_factory=dict)
```

and append:

```python
def safe_segment(value: str) -> str:
    """Confine an opaque id to one path segment exactly as the TUI's ``sanitizeRunId`` does.

    JavaScript replaces per UTF-16 code unit, so a character outside the BMP
    becomes two underscores there and must here too.
    """
    return "".join(
        "_" * (2 if ord(character) > _BMP_LAST else 1)
        if _UNSAFE_SEGMENT.fullmatch(character)
        else character
        for character in value
    )
```

- [ ] **Step 6: Implement start**

`src/entrypoints/web_home/runs.py`:

```python
"""Run history with gateway states, and launching, reopening, resuming, and stopping runs.

Discovery records for run servers this home server launches live under
``$VIBESYS_STATE_HOME/web/gateways/<project id>/`` so they never dirty the
candidate repository. A run started elsewhere (the TUI with ``--web``,
``vibesys web live``) publishes the default ``.vibesys/web-gateway.json`` and
is reported as ``external``.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from entrypoints.web_home.catalog import LOOPS
from entrypoints.web_home.context import atomic_write, parse_body, safe_segment, validation_errors
from entrypoints.web_home.contract import (
    ApiError,
    ErrorCode,
    Gateway,
    GatewayState,
    LaunchResult,
    ProjectState,
    StartRun,
)
from entrypoints.web_home.projects import inspect_project, project_id, resolve_project
from entrypoints.web_home.tasks import select_task
from server.runtime import WebInstanceRecord
from vibesys.api import Config
from vibesys.api.request import generate_experiment_name, load_project_task
from vs_agent.api import SHIPPED_PROVIDERS, agent_catalog
from vs_project.api import Project, ProjectError

if TYPE_CHECKING:
    from pathlib import Path

    from entrypoints.web_home.context import HomeConfig, Request

_RECORD_POLL_SECONDS = 0.05
_REAP_SECONDS = 5.0
_STDERR_TAIL_BYTES = 16_384
_STDERR_TAIL_LINES = 40
_BLOCKERS = {
    ProjectState.MISSING: ErrorCode.INVALID_PATH,
    ProjectState.NOT_GIT: ErrorCode.NOT_GIT,
    ProjectState.INVALID: ErrorCode.TASK_INVALID,
    ProjectState.UNINITIALIZED: ErrorCode.UNINITIALIZED,
    ProjectState.NO_TASKS: ErrorCode.NO_TASKS,
    ProjectState.NO_COMMITS: ErrorCode.NO_COMMITS,
    ProjectState.DIRTY_TREE: ErrorCode.DIRTY_TREE,
}


@dataclass(frozen=True)
class Launch:
    """A run server this process spawned; a daemon thread reaps it when it exits."""

    run_id: str
    process: subprocess.Popen[bytes]
    stderr_log: Path


class Owner(BaseModel):
    """Sidecar ``<record>.owner.json``: which launch published a home-owned record.

    Written atomically after each spawn. Plan 1's live records carry no run id,
    and the pid ties the record to exactly one launch attempt, so a record from
    an earlier attempt is never attributed to a retry.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    pid: int
    origin: str


def _gateway_dir(config: HomeConfig, root: Path) -> Path:
    return config.state_home / "web" / "gateways" / project_id(root)


def _live_record(config: HomeConfig, root: Path) -> Path:
    return _gateway_dir(config, root) / "live.json"


def _run_config(config: HomeConfig, root: Path, run_id: str) -> Path:
    return _gateway_dir(config, root) / f"{safe_segment(run_id)}.agent.toml"


def _external_record(project: Project) -> Path:
    return project.configuration_path() / "web-gateway.json"


def _owner_path(record_path: Path) -> Path:
    return record_path.with_suffix(".owner.json")


def _read_owner(record_path: Path) -> Owner | None:
    try:
        return Owner.model_validate_json(_owner_path(record_path).read_bytes())
    except (OSError, ValidationError):
        return None


def stderr_tail(log: Path) -> list[str]:
    """Return the last lines a run server wrote to stderr."""
    try:
        size = log.stat().st_size
        with log.open("rb") as stream:
            stream.seek(max(0, size - _STDERR_TAIL_BYTES))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return []
    return text.splitlines()[-_STDERR_TAIL_LINES:]


def _connected(
    record: WebInstanceRecord, state: GatewayState, *, mismatch: bool = False
) -> Gateway:
    return Gateway(
        state=state,
        url=record.url,
        websocket_url=f"ws://127.0.0.1:{record.port}/ws?token={record.token}",
        token=record.token,
        origin_mismatch=mismatch,
    )


def _failure(log: Path, message: str) -> ApiError:
    details: dict[str, JsonValue] = {"stderr_tail": [*stderr_tail(log)], "stderr_log": str(log)}
    return ApiError(ErrorCode.LAUNCH_FAILED, message, details=details)


def _reap(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _spawn(
    config: HomeConfig, root: Path, record_path: Path, run_id: str, arguments: list[str]
) -> WebInstanceRecord:
    """Start a detached run server; return once it publishes *record_path* (gateway ready).

    A child that exits first, or publishes nothing within ``config.launch_timeout``,
    is reported as ``launch_failed``; a timed-out child is terminated and reaped
    before the error returns, so a retry never races it.
    """
    record_path.parent.mkdir(parents=True, exist_ok=True)
    _owner_path(record_path).unlink(missing_ok=True)
    origins = (config.origin, *config.dev_origins)
    argv = [
        *config.run_server_argv,
        *("--web", "--detach", "--web-port", "0", "--web-instance", str(record_path)),
        *(item for origin in origins for item in ("--web-origin", origin)),
        *arguments,
    ]
    # VIBESYS_DETACHED_CHILD makes entrypoints.server serve in this child instead of
    # re-spawning with stderr discarded; BROWSER=true stops it opening a browser tab.
    environment = {**config.environ, "VIBESYS_DETACHED_CHILD": "1", "BROWSER": "true"}
    log = record_path.with_suffix(".stderr.log")
    with log.open("wb") as stderr:
        process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-101304 [S603]; argv is the fixed run-server command plus validated flags, never a shell string.
            # > `entrypoints.server --detach` re-spawns with stderr discarded, and the
            # > launch_failed contract returns that stderr; shell=True weakens argv safety.
            argv,
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            start_new_session=True,
        )
    threading.Thread(target=process.wait, name=f"reap-{process.pid}", daemon=True).start()
    owner = Owner(run_id=run_id, pid=process.pid, origin=config.origin)
    atomic_write(_owner_path(record_path), owner.model_dump_json().encode(), mode=0o600)
    config.launches[record_path] = Launch(run_id=run_id, process=process, stderr_log=log)
    deadline = time.monotonic() + config.launch_timeout
    while time.monotonic() < deadline:
        record = WebInstanceRecord.discover(record_path, cleanup_stale=False)
        if record is not None and record.pid == process.pid:
            return record
        if process.poll() is not None:
            raise _failure(log, f"the run server exited with status {process.returncode}")
        time.sleep(_RECORD_POLL_SECONDS)
    _reap(process)
    raise _failure(log, f"the run server published no gateway within {config.launch_timeout:g} s")


def _owner_run(
    path: Path, record: WebInstanceRecord | None, project: Project, *, external: bool
) -> str | None:
    """Return the run a live record belongs to; live records carry no run id themselves."""
    if external:
        with contextlib.suppress(ProjectError):
            return project.state.current_run_id()
        return None
    owner = _read_owner(path)
    if owner is None or (record is not None and record.pid != owner.pid):
        return None
    return owner.run_id


def _require_no_live(config: HomeConfig, root: Path) -> None:
    project = Project.open(root)
    for path, external in ((_live_record(config, root), False), (_external_record(project), True)):
        record = WebInstanceRecord.discover(path)
        if record is not None:
            message = "this project already has a live run; stop it first"
            run_id = _owner_run(path, record, project, external=external)
            raise ApiError(ErrorCode.ALREADY_LIVE, message, details={"run_id": run_id})


def _require_ready(root: Path) -> None:
    validation = inspect_project(root)
    if validation.state is not ProjectState.READY:
        message = f"the project is not ready to launch: {validation.state.value}"
        raise ApiError(
            _BLOCKERS[validation.state], message, details={"pending": [*validation.pending]}
        )


def render_run_config(body: StartRun) -> str:
    """Render the run-owned agent TOML passed with ``--config``, validated as ``Config``."""
    agent: dict[str, object] = {"backend": "cli", "cli_provider": body.provider}
    if body.driver is not None:
        agent["driver"] = body.driver.value
    roles = {
        role: fields
        for role, override in sorted(body.roles.items())
        if (fields := override.model_dump(exclude_none=True))
    }
    raw: dict[str, object] = {"model": {"name": body.model}, "agent": {**agent, "roles": roles}}
    if body.reasoning_effort is not None:
        raw["thinking"] = {"level": body.reasoning_effort}
    try:
        Config.model_validate(raw)
    except ValidationError as error:
        message = "the run configuration is invalid"
        raise ApiError(
            ErrorCode.INVALID_REQUEST, message, details={"errors": validation_errors(error)}
        ) from None
    lines = ["[model]", f"name = {json.dumps(body.model)}"]
    if body.reasoning_effort is not None:
        lines += ["", "[thinking]", f"level = {json.dumps(body.reasoning_effort)}"]
    lines += ["", "[agent]", *(f"{key} = {json.dumps(value)}" for key, value in agent.items())]
    for role, fields in roles.items():
        lines += ["", f"[agent.roles.{json.dumps(role)}]"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in fields.items()]
    return "\n".join(lines) + "\n"


def _check_start(body: StartRun) -> None:
    if body.outer_loop not in LOOPS:
        message = f"unknown outer loop {body.outer_loop!r}; choose from {', '.join(LOOPS)}"
        raise ApiError(ErrorCode.INVALID_REQUEST, message)
    providers = (
        agent_catalog()[body.driver].providers if body.driver is not None else SHIPPED_PROVIDERS
    )
    if body.provider not in providers:
        message = f"provider {body.provider!r} is not available for this driver"
        raise ApiError(ErrorCode.UNKNOWN_PROVIDER, message)


def _start_arguments(body: StartRun, root: Path, run_id: str, run_config: Path) -> list[str]:
    arguments = [
        *("--project", str(root), "--task", body.task, "--outer-loop", body.outer_loop),
        *("--exp-name", run_id, "--config", str(run_config)),
        *("--backend", body.compute_backend.value, "--cli-provider", body.provider),
    ]
    if body.budget is not None:
        arguments += [LOOPS[body.outer_loop][0], str(body.budget)]
    return arguments


def start_run(request: Request) -> LaunchResult:
    """``POST /api/projects/{id}/runs``: launch a run; return once its gateway is ready."""
    body = parse_body(request, StartRun)
    _check_start(body)
    config = request.config
    root = resolve_project(config, request.params[0])
    with config.launch_lock:
        _require_no_live(config, root)
        _require_ready(root)
        project = Project.open(root)
        manifest = load_project_task(project, select_task(project, body.task)).manifest
        if body.outer_loop == "profile-guided" and manifest.profile_guided is None:
            message = (
                "the profile-guided loop needs a [profile_guided] section in the task manifest"
            )
            raise ApiError(ErrorCode.PROFILE_GUIDED_UNAVAILABLE, message)
        run_id = generate_experiment_name(root)
        run_config = _run_config(config, root, run_id)
        atomic_write(run_config, render_run_config(body).encode("utf-8"), mode=0o600)
        arguments = _start_arguments(body, root, run_id, run_config)
        record = _spawn(config, root, _live_record(config, root), run_id, arguments)
    return LaunchResult(run_id=run_id, gateway=_connected(record, GatewayState.STARTING))
```

In `app.py`, import `runs` and add `("POST", re.compile(_PROJECT + r"/runs"), runs.start_run),`.

- [ ] **Step 7: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_runs.py -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/entrypoints/web_home tests/entrypoints/web_home
git commit -m "feat(web): start runs as detached run servers and report launch failures"
```

### Task 13: Resume, open, and stop

**Files:**
- Modify: `src/entrypoints/web_home/runs.py`, `src/entrypoints/web_home/app.py`
- Test: `tests/entrypoints/web_home/test_home_runs.py`

**Interfaces:**
- Produces: `resume_run`, `open_run`, `stop_live` handlers; `runs._reopen_arguments(root, run_id) -> list[str]` (the only place that knows sub-project 1's reopen flags: `--project <root> --web-reopen-run <run>`); `runs._serving_reopen(config, root, run_id) -> WebInstanceRecord | None`.

- [ ] **Step 1: Append the failing tests**

Add `import subprocess`, `from tests.support.run_execution import run_execution_record`, and `from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord` to the imports, then append:

```python
def _persist_run(root: Path, run_id: str, *, max_rounds: int = 3) -> None:
    project = Project.open(root)
    project.state.create_project("proj")
    manifest = project.state.new_run_manifest(
        run_id,
        run_id=run_id,
        branch=f"vibesys-runs/{run_id}",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="plain",
            config_version=1,
            options={
                "max_rounds": max_rounds,
                "max_attempts_per_issue": 2,
                "max_issues_per_perf_eval": 2,
            },
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)


def test_resume_keeps_the_recorded_loop_and_refuses_a_smaller_budget(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run", max_rounds=3)

    smaller = runs_home.post(f"/api/projects/{key}/runs/plain-run/resume", {"budget": 2}).json()
    resumed = runs_home.post(f"/api/projects/{key}/runs/plain-run/resume", {"budget": 5}).json()

    argv = _argv(runs_home)
    assert smaller["error"]["code"] == "budget_decrease"
    assert resumed["run_id"] == "plain-run"
    assert argv[argv.index("--resume") + 1] == "plain-run"
    assert argv[argv.index("--outer-loop") + 1] == "plain"
    assert argv[argv.index("--max-rounds") + 1] == "5"
    missing = runs_home.post(f"/api/projects/{key}/runs/ghost/resume", {}).json()
    assert missing["error"]["code"] == "unknown_run"


def test_open_serves_a_run_read_only_beside_the_live_one(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")

    first = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()
    again = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()

    argv = _argv(runs_home, "reopen-plain-run")
    assert first["gateway"]["state"] == "reopened"
    assert again["gateway"]["url"] == first["gateway"]["url"]
    assert argv[argv.index("--web-reopen-run") + 1] == "plain-run"
    assert argv[argv.index("--project") + 1] == str(root)
    assert "--web-reopen" not in argv


def test_a_reopen_published_by_another_launcher_is_reused(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")
    record = Project.open(root).configuration_path() / "web-gateway-plain-run.json"
    process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-101308 [S603]; the test starts its own fake gateway script with fixed arguments.
        # > run_test_command waits for exit, but this gateway must keep serving while the
        # > test queries the API; a shell wrapper would add quoting for no gain.
        [sys.executable, str(FAKE), "--web-instance", str(record), "--web-reopen-run", "plain-run"],
        cwd=root,
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"
        opened = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()
        assert opened["gateway"]["url"] == json.loads(record.read_text())["url"]
        assert runs_home.config.launches == {}
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_stop_terminates_the_live_gateway_once(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    started = runs_home.post(f"/api/projects/{key}/runs", START).json()

    stopped = runs_home.delete(f"/api/projects/{key}/live").json()
    next(iter(runs_home.config.launches.values())).process.wait(timeout=10)

    assert stopped == {"stopped": True, "run_id": started["run_id"]}
    assert runs_home.delete(f"/api/projects/{key}/live").json() == {
        "stopped": False,
        "run_id": None,
    }
```

Add one test against the real run server, `tests/entrypoints/web_home/test_home_real_gateway.py`, so the fake's token and Origin rules cannot drift from `websocket.py`. It uses sub-project 1's `finished_run` helper and `--web-reopen-run`:

```python
"""The home server against the real run server: token and Origin compatibility.

Needs sub-project 1 (`--web-reopen-run`, `tests/server/support.finished_run`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from tests.entrypoints.web_home.support import project_key
from tests.server.support import finished_run
from tests.support import run_test_command

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home


def test_a_real_reopen_gateway_accepts_only_the_app_origin_and_token(home: Home) -> None:
    project, run_id, _log_dir = finished_run(home.workspace / "proj")
    run_test_command(["git", "init", "-q"], cwd=project.root, check=True)
    key = project_key(home, project.root)
    try:
        reply = home.post(f"/api/projects/{key}/runs/{run_id}/open")
        gateway = reply.json()["gateway"]
        url = gateway["websocket_url"]

        with connect(url, origin=home.config.origin, open_timeout=10):
            pass
        with connect(url, origin="http://127.0.0.1:5173", open_timeout=10):
            pass
        with pytest.raises(InvalidStatus), connect(url, origin="http://evil.test"):
            pass
        forged = url.replace(gateway["token"], "wrong")
        with pytest.raises(InvalidStatus), connect(forged, origin=home.config.origin):
            pass
        assert reply.status == 200
    finally:
        for launch in home.config.launches.values():
            launch.process.terminate()
            launch.process.wait(timeout=30)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_runs.py -q --basetemp=/tmp/vsw`
Expected: the new tests FAIL with `not_found`.

- [ ] **Step 3: Implement**

In `runs.py` add `import os`, `import signal`; `from entrypoints.cli.resume import _POLICY_CLI_SELECTION`; import `budget_destination` from `catalog`; add `ResumeRun`, `StopResult` to the contract import; `ProjectError` to the `vs_project.api` import; and `from vs_project.api import OrchestrationRunManifest` under `TYPE_CHECKING`. Append:

```python
def _reopen_record(config: HomeConfig, root: Path, run_id: str) -> Path:
    return _gateway_dir(config, root) / f"reopen-{safe_segment(run_id)}.json"


def _load_run(root: Path, run_id: str) -> OrchestrationRunManifest:
    try:
        return Project.open(root).state.load_run(run_id)
    except (ProjectError, ValueError):
        message = f"no run {run_id!r} in this project"
        raise ApiError(ErrorCode.UNKNOWN_RUN, message) from None


def resume_run(request: Request) -> LaunchResult:
    """``POST .../runs/{run}/resume``: resume; the CLI restores the recorded configuration."""
    body = parse_body(request, ResumeRun)
    config = request.config
    root = resolve_project(config, request.params[0])
    run_id = request.params[1]
    with config.launch_lock:
        descriptor = _load_run(root, run_id).orchestration
        if descriptor.id not in _POLICY_CLI_SELECTION:
            message = f"the app cannot resume orchestration {descriptor.id!r}"
            raise ApiError(ErrorCode.NOT_RESUMABLE, message)
        loop = _POLICY_CLI_SELECTION[descriptor.id][0]
        flag = LOOPS[loop][0]
        recorded = descriptor.options.get(budget_destination(flag))
        if body.budget is not None and isinstance(recorded, int) and body.budget < recorded:
            message = f"{flag} is the run's total limit and cannot go below {recorded}"
            raise ApiError(ErrorCode.BUDGET_DECREASE, message, details={"recorded": recorded})
        _require_no_live(config, root)
        arguments = ["--project", str(root), "--resume", run_id, "--outer-loop", loop]
        if body.budget is not None:
            arguments += [flag, str(body.budget)]
        record = _spawn(config, root, _live_record(config, root), run_id, arguments)
    return LaunchResult(run_id=run_id, gateway=_connected(record, GatewayState.STARTING))


def _reopen_arguments(root: Path, run_id: str) -> list[str]:
    # Sub-project 1: `--web-reopen-run` with `--project` reads the run's own journal and record.
    return ["--project", str(root), "--web-reopen-run", run_id]


def _serving_reopen(
    config: HomeConfig, root: Path, run_id: str
) -> tuple[WebInstanceRecord, bool] | None:
    """Return a serving read-only gateway for *run_id* and whether it rejects our origin.

    Ours is checked first, then plan 1's default record from another launcher.
    """
    external = _external_record(Project.open(root)).with_name(f"web-gateway-{run_id}.json")
    for path in (_reopen_record(config, root, run_id), external):
        record = WebInstanceRecord.discover(path) if path.exists() else None
        if record is not None and (record.mode, record.run_id) == ("reopen", run_id):
            owner = _read_owner(path) if path != external else None
            return record, owner is not None and owner.origin != config.origin
    return None


def _stop_and_wait(record: WebInstanceRecord, path: Path) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + _REAP_SECONDS
    while path.exists() and time.monotonic() < deadline:
        time.sleep(_RECORD_POLL_SECONDS)


def open_run(request: Request) -> LaunchResult:
    """``POST .../runs/{run}/open``: serve a run read-only, reusing a gateway that still serves.

    A reopen gateway started for an older home origin is restarted, since it
    would reject the app.
    """
    config = request.config
    root = resolve_project(config, request.params[0])
    run_id = request.params[1]
    with config.launch_lock:
        _load_run(root, run_id)
        path = _reopen_record(config, root, run_id)
        serving = _serving_reopen(config, root, run_id)
        if serving is not None and serving[1]:
            _stop_and_wait(serving[0], path)
            serving = None
        if serving is None:
            record = _spawn(config, root, path, run_id, _reopen_arguments(root, run_id))
        else:
            record = serving[0]
    return LaunchResult(run_id=run_id, gateway=_connected(record, GatewayState.REOPENED))


def stop_live(request: Request) -> StopResult:
    """``DELETE /api/projects/{id}/live``: SIGTERM the live gateway, as ``web stop`` does."""
    config = request.config
    root = resolve_project(config, request.params[0])
    project = Project.open(root)
    for path, external in ((_live_record(config, root), False), (_external_record(project), True)):
        record = WebInstanceRecord.discover(path)
        if record is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(record.pid, signal.SIGTERM)
            run_id = _owner_run(path, record, project, external=external)
            return StopResult(stopped=True, run_id=run_id)
    return StopResult(stopped=False, run_id=None)
```

Routes in `app.py`:

```python
    ("POST", re.compile(_PROJECT + r"/runs/([^/]+)/open"), runs.open_run),
    ("POST", re.compile(_PROJECT + r"/runs/([^/]+)/resume"), runs.resume_run),
    ("DELETE", re.compile(_PROJECT + r"/live"), runs.stop_live),
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_runs.py tests/entrypoints/web_home/test_home_real_gateway.py -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/entrypoints/web_home tests/entrypoints/web_home
git commit -m "feat(web): resume, reopen, and stop runs"
```

### Task 14: Run list with gateway states

**Files:**
- Modify: `src/entrypoints/web_home/runs.py`, `src/entrypoints/web_home/app.py`
- Test: `tests/entrypoints/web_home/test_home_runs.py`

- [ ] **Step 1: Append the failing tests**

Change the typing import to `from typing import TYPE_CHECKING, Any` and the `TYPE_CHECKING` import to `from collections.abc import Iterator, Mapping`, then append:

```python
def _rows(home: Home, key: str) -> dict[str, dict[str, Any]]:
    return {row["run_id"]: row for row in home.get(f"/api/projects/{key}/runs").json()["runs"]}


def _journal(root: Path, run_id: str, *events: Mapping[str, object]) -> None:
    journal = Project.log_directory_for(root, run_id) / "run-events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a") as stream:
        stream.writelines(json.dumps(event) + "\n" for event in events)


def _state(home: Home, key: str, run_id: str) -> tuple[str, str]:
    row = _rows(home, key)[run_id]
    return row["status"], row["gateway"]["state"]


def _stop(home: Home, key: str) -> dict[str, Any]:
    stopped = home.delete(f"/api/projects/{key}/live").json()
    for launch in home.config.launches.values():
        launch.process.wait(timeout=10)
    return stopped


def test_run_list_reports_starting_and_failed_launches(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    started = runs_home.post(f"/api/projects/{key}/runs", START).json()
    assert _rows(runs_home, key)[started["run_id"]]["gateway"]["state"] == "starting"
    runs_home.delete(f"/api/projects/{key}/live")
    next(iter(runs_home.config.launches.values())).process.wait(timeout=10)
    runs_home.config.environ = {**runs_home.config.environ, "FAKE_RUN_SERVER_FAIL": "1"}

    runs_home.post(f"/api/projects/{key}/runs", START)

    [row] = runs_home.get(f"/api/projects/{key}/runs").json()["runs"]
    assert (row["status"], row["gateway"]["state"]) == ("failed", "failed")
    assert row["gateway"]["stderr_tail"][-1] == "ConfigurationError: bad run"
    assert row["gateway"]["stderr_log"].endswith("live.stderr.log")


def test_run_list_follows_the_latest_attempt_in_the_journal(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")
    assert _state(runs_home, key, "plain-run") == ("unknown", "none")

    runs_home.post(f"/api/projects/{key}/runs/plain-run/resume", {})
    assert _state(runs_home, key, "plain-run") == ("active", "starting")
    attached = {"type": "experiments_changed", "data": {"reason": "project_attached"}}
    _journal(root, "plain-run", {"type": "server_started"}, {"type": "run_started"}, attached)
    assert _state(runs_home, key, "plain-run") == ("active", "live")
    _journal(root, "plain-run", {"type": "run_finished", "status": "completed"})
    assert _state(runs_home, key, "plain-run") == ("completed", "ended_serving")

    assert _stop(runs_home, key) == {"stopped": True, "run_id": "plain-run"}
    assert _state(runs_home, key, "plain-run") == ("completed", "none")
    assert _stop(runs_home, key) == {"stopped": False, "run_id": None}

    runs_home.post(f"/api/projects/{key}/runs/plain-run/resume", {"budget": 4})
    _journal(root, "plain-run", {"type": "server_started"})
    assert _state(runs_home, key, "plain-run") == ("active", "starting")
    _journal(root, "plain-run", attached)
    assert _state(runs_home, key, "plain-run") == ("active", "live")
    _journal(root, "plain-run", {"type": "run_failed", "status": "failed"})
    assert _state(runs_home, key, "plain-run") == ("failed", "ended_serving")
    _stop(runs_home, key)
    assert _state(runs_home, key, "plain-run") == ("failed", "none")


def test_run_list_shows_a_serving_reopen_beside_the_run(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")

    runs_home.post(f"/api/projects/{key}/runs/plain-run/open")

    row = _rows(runs_home, key)["plain-run"]
    assert (row["loop"], row["gateway"]["state"], row["reopen"]["state"]) == (
        "plain",
        "none",
        "reopened",
    )
    # The setup UI titles sidebar rows from these (plan 4); a stored run always has a manifest time.
    assert row["created_at"] is not None
    assert {"task", "objective"} <= row.keys()


def test_a_reopen_serving_an_old_home_origin_is_restarted(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")
    first = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()["gateway"]
    gateways = runs_home.config.state_home / "web" / "gateways"
    owner_path = next(gateways.glob("*/reopen-plain-run.owner.json"))
    owner = json.loads(owner_path.read_text())
    owner_path.write_text(json.dumps({**owner, "origin": "http://127.0.0.1:1"}))
    assert _rows(runs_home, key)["plain-run"]["reopen"]["origin_mismatch"] is True

    second = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()["gateway"]

    assert second["url"] != first["url"]
    assert _rows(runs_home, key)["plain-run"]["reopen"]["origin_mismatch"] is False


def test_an_external_gateway_and_a_stale_record_are_reported(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")
    external = Project.open(root).configuration_path() / "web-gateway.json"
    process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-101306 [S603]; the test starts its own fake gateway script with fixed arguments.
        # > run_test_command waits for exit, but this gateway must keep serving while the
        # > test queries the API; a shell wrapper would add quoting for no gain.
        [sys.executable, str(FAKE), "--web-instance", str(external), "--exp-name", "plain-run"],
        cwd=root,
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"
        assert _rows(runs_home, key)["plain-run"]["gateway"]["state"] == "external"
        assert (
            runs_home.post(f"/api/projects/{key}/runs", START).json()["error"]["code"]
            == "already_live"
        )
    finally:
        process.terminate()
        process.wait(timeout=10)
    stale = {
        "version": 1,
        "pid": 999_999,
        "port": 9,
        "token": "t",
        "url": "http://127.0.0.1:9/?token=t",
        "project_root": str(root),
        "started_at": 0,
        "mode": "live",
    }
    external.write_text(json.dumps(stale))
    assert _rows(runs_home, key)["plain-run"]["gateway"]["state"] == "stale"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_runs.py -q --basetemp=/tmp/vsw`
Expected: the new tests FAIL with `not_found`.

- [ ] **Step 3: Implement**

In `runs.py`, add `RunList` and `RunRow` to the contract import, change the core import to `from vibesys.api import Config, RunRecordReadError, RunStatus, open_run_store`, add `from vibesys.api import RunStore` under `TYPE_CHECKING` (beside Task 13's `OrchestrationRunManifest`), add the constants

```python
_HISTORY_TAIL_BYTES = 262_144
_ATTEMPT_MARKERS = (
    b'"server_started"',
    b'"experiments_changed"',
    b'"run_finished"',
    b'"run_failed"',
    b'"run_interrupted"',
)
# The controller records a failed run as `run_failed`, not `run_finished` with a failed status.
_TERMINAL = {
    "run_finished": RunStatus.COMPLETED,
    "run_failed": RunStatus.FAILED,
    "run_interrupted": RunStatus.FAILED,
}
```

and append:

```python
@dataclass(frozen=True)
class _Published:
    """One live discovery record file; ``record`` is ``None`` when its gateway does not answer."""

    path: Path
    record: WebInstanceRecord | None
    run_id: str | None
    external: bool
    origin_mismatch: bool


def origin_mismatches(config: HomeConfig) -> list[str]:
    """Describe serving home-launched gateways that only accept another home origin."""
    found: list[str] = []
    for owner_path in sorted((config.state_home / "web" / "gateways").glob("*/*.owner.json")):
        record_path = owner_path.with_name(owner_path.name.removesuffix(".owner.json") + ".json")
        owner = _read_owner(record_path)
        record = WebInstanceRecord.discover(record_path, cleanup_stale=False)
        if owner is not None and record is not None and owner.origin != config.origin:
            found.append(f"{record.project_root} run {owner.run_id} ({owner.origin})")
    return found


def _published(config: HomeConfig, root: Path, project: Project) -> list[_Published]:
    items: list[_Published] = []
    for path, external in ((_live_record(config, root), False), (_external_record(project), True)):
        if not path.exists():
            continue
        record = WebInstanceRecord.discover(path, cleanup_stale=False)
        if record is not None and record.mode != "live":
            continue
        owner = None if external else _read_owner(path)
        items.append(
            _Published(
                path=path,
                record=record,
                run_id=_owner_run(path, record, project, external=external),
                external=external,
                origin_mismatch=owner is not None and owner.origin != config.origin,
            )
        )
    return items


def _attempt(
    root: Path, run_id: str, *, tail_bytes: int | None = None
) -> tuple[bool, RunStatus | None]:
    """Return (record attached, terminal status) of the journal's latest server attempt.

    Every run server starts its journal segment with ``server_started`` (a resume
    appends a new one), so earlier attempts' ``run_finished`` never count. The run
    is attached once ``experiments_changed`` reports ``project_attached``
    (sub-project 1 emits it after the run record attaches).
    """
    # ponytail: rescans the journal per call (the live run full, history rows the tail);
    # keep a per-journal byte offset if large journals make polling slow.
    attached, terminal = False, None
    try:
        # ponytail: log_directory_for prepares the state home on every run list; harmless
        # (idempotent mkdir), switch to a read-only path lookup if it ever shows up in profiles.
        journal = Project.log_directory_for(root, run_id) / "run-events.jsonl"
        size = journal.stat().st_size
        with journal.open("rb") as stream:
            start = 0 if tail_bytes is None else max(0, size - tail_bytes)
            stream.seek(start)
            lines = stream.read().splitlines()
    except (OSError, ProjectError):
        return False, None
    for line in lines[1:] if start else lines:
        if not any(marker in line for marker in _ATTEMPT_MARKERS):
            continue
        event = _event(line)
        kind = event.get("type")
        if kind == "server_started":
            attached, terminal = False, None
        elif kind == "experiments_changed":
            data = event.get("data")
            attached = attached or (
                isinstance(data, dict) and data.get("reason") == "project_attached"
            )
        elif isinstance(kind, str) and kind in _TERMINAL:
            terminal = _TERMINAL[kind]
    return attached, terminal


def _event(line: bytes) -> dict[str, object]:
    try:
        event = json.loads(line)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def _published_gateway(item: _Published, root: Path, run_id: str) -> Gateway:
    if item.record is None:
        return Gateway(state=GatewayState.STALE)
    if item.external:
        return _connected(item.record, GatewayState.EXTERNAL)
    attached, terminal = _attempt(root, run_id)
    if terminal is not None:
        state = GatewayState.ENDED_SERVING
    else:
        state = GatewayState.LIVE if attached else GatewayState.STARTING
    return _connected(item.record, state, mismatch=item.origin_mismatch)


def _launch_gateway(config: HomeConfig, root: Path, run_id: str) -> Gateway:
    launch = config.launches.get(_live_record(config, root))
    if launch is None or launch.run_id != run_id:
        return Gateway(state=GatewayState.NONE)
    code = launch.process.poll()
    if code is None:
        return Gateway(state=GatewayState.STARTING)
    # A positive status is the run server failing; a negative one is a signal, e.g. our SIGTERM.
    if code > 0:
        return Gateway(
            state=GatewayState.FAILED,
            stderr_tail=stderr_tail(launch.stderr_log),
            stderr_log=str(launch.stderr_log),
        )
    return Gateway(state=GatewayState.NONE)


def _gateway(config: HomeConfig, root: Path, run_id: str, published: list[_Published]) -> Gateway:
    for item in published:
        if item.run_id == run_id:
            return _published_gateway(item, root, run_id)
    return _launch_gateway(config, root, run_id)


def _reopen_gateway(config: HomeConfig, root: Path, run_id: str) -> Gateway | None:
    serving = _serving_reopen(config, root, run_id)
    if serving is None:
        return None
    return _connected(serving[0], GatewayState.REOPENED, mismatch=serving[1])


def _status(root: Path, run_id: str, gateway: Gateway) -> RunStatus:
    """Derive lifecycle from the journal and the gateway; the store always says unknown."""
    if gateway.state is GatewayState.FAILED:
        return RunStatus.FAILED
    if gateway.state in {GatewayState.LIVE, GatewayState.STARTING, GatewayState.EXTERNAL}:
        return RunStatus.ACTIVE
    terminal = _attempt(root, run_id, tail_bytes=_HISTORY_TAIL_BYTES)[1]
    return terminal or RunStatus.UNKNOWN


def _row(
    config: HomeConfig,
    root: Path,
    store: RunStore,
    manifest: OrchestrationRunManifest,
    published: list[_Published],
) -> RunRow:
    run_id = manifest.run_id
    gateway = _gateway(config, root, run_id, published)
    reopen = _reopen_gateway(config, root, run_id)
    status = _status(root, run_id, gateway)
    identity = {"task": manifest.task_name, "created_at": manifest.created_at.isoformat()}
    try:
        view = store.get_run(run_id)
        objective = store.get_record(run_id).facts().effective_objective
    except (RunRecordReadError, ProjectError, ValueError) as error:
        return RunRow(
            run_id=run_id,
            loop=None,
            status=status,
            rounds=0,
            gateway=gateway,
            reopen=reopen,
            error=str(error),
            **identity,
        )
    return RunRow(
        run_id=run_id,
        loop=view.loop,
        status=status,
        rounds=len(view.rounds),
        gateway=gateway,
        reopen=reopen,
        objective=objective,
        **identity,
    )


def list_runs(request: Request) -> RunList:
    """``GET /api/projects/{id}/runs``: runs newest first, each with its gateway state."""
    config = request.config
    root = resolve_project(config, request.params[0])
    project = Project.open(root)
    published = _published(config, root, project)
    try:
        manifests = project.state.list_runs()
    except ProjectError:
        manifests = []
    store = open_run_store(project)
    rows = [
        _row(config, root, store, manifest, published) for manifest in reversed(manifests)
    ]
    known = {row.run_id for row in rows}
    launch = config.launches.get(_live_record(config, root))
    candidates = [item.run_id for item in published] + ([launch.run_id] if launch else [])
    pending = [run_id for run_id in dict.fromkeys(candidates) if run_id and run_id not in known]
    gateways = [(run_id, _gateway(config, root, run_id, published)) for run_id in pending]
    # Launches not yet in the run store have no manifest or record, so task, objective
    # and created_at stay None.
    starting = [
        RunRow(
            run_id=run_id,
            loop=None,
            status=RunStatus.FAILED if gateway.state is GatewayState.FAILED else RunStatus.ACTIVE,
            rounds=0,
            gateway=gateway,
        )
        for run_id, gateway in gateways
        if gateway.state is not GatewayState.NONE
    ]
    return RunList(runs=[*starting, *rows])
```

In `app.py`'s `run_home`, replace the port-saving `if` with the version that also reports gateways the new origin strands:

```python
    previous = saved_port(web_dir)
    if config.port != previous:
        save_port(web_dir, config.port)
        for gateway in runs.origin_mismatches(config):
            _LOG.warning("gateway %s allows only the old origin; stop and reopen it", gateway)
```

Route in `app.py`: `("GET", re.compile(_PROJECT + r"/runs"), runs.list_runs),`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home/test_home_runs.py -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/entrypoints/web_home tests/entrypoints/web_home/test_home_runs.py
git commit -m "feat(web): list runs with live, starting, ended, failed, stale, and external gateways"
```

### Task 15: Notes

**Files:**
- Create: `src/entrypoints/web_home/notes.py`
- Modify: `src/entrypoints/web_home/app.py` (final route table)
- Test: `tests/entrypoints/web_home/test_home_notes.py`

- [ ] **Step 1: Write the failing tests**

`tests/entrypoints/web_home/test_home_notes.py`:

```python
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home


def test_notes_round_trip_in_the_tui_file_format(home: Home) -> None:
    assert home.get("/api/notes/run-1").json() == {"note": None}

    saved = home.put("/api/notes/run-1", {"text": "check the p99"}).json()["note"]

    path = home.config.state_home / "tui" / "notes" / "run-1.json"
    assert json.loads(path.read_text()) == {
        "runId": "run-1",
        "text": "check the p99",
        "createdAt": "2026-09-28T12:00:00.000Z",
        "updatedAt": "2026-09-28T12:00:00.000Z",
    }
    assert saved == json.loads(path.read_text())
    assert home.get("/api/notes/run-1").json()["note"] == saved


def test_a_note_written_by_the_tui_is_read_and_its_creation_time_kept(home: Home) -> None:
    path = home.config.state_home / "tui" / "notes" / "run-2.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "runId": "run-2",
                "text": "old",
                "createdAt": "2026-01-01T00:00:00.000Z",
                "updatedAt": "2026-01-01T00:00:00.000Z",
            }
        )
    )

    saved = home.put("/api/notes/run-2", {"text": "new"}).json()["note"]

    assert (saved["createdAt"], saved["updatedAt"], saved["text"]) == (
        "2026-01-01T00:00:00.000Z",
        "2026-09-28T12:00:00.000Z",
        "new",
    )


def test_run_ids_map_to_the_same_file_name_as_the_tui(home: Home) -> None:
    home.put("/api/notes/a%2Fb%20c%F0%9F%98%80", {"text": "x"})

    names = sorted(p.name for p in (home.config.state_home / "tui" / "notes").iterdir())
    assert names == ["a_b_c__.json"]


def test_a_corrupt_note_reads_as_absent(home: Home) -> None:
    path = home.config.state_home / "tui" / "notes" / "run-3.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    assert home.get("/api/notes/run-3").json() == {"note": None}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/entrypoints/web_home/test_home_notes.py -q --basetemp=/tmp/vsw`
Expected: FAIL, `not_found`.

- [ ] **Step 3: Implement**

`src/entrypoints/web_home/notes.py`:

```python
"""Run notes shared with the TUI (``$VIBESYS_STATE_HOME/tui/notes/<run>.json``, last write wins)."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from pydantic import ValidationError

from entrypoints.web_home.context import atomic_write, parse_body, safe_segment
from entrypoints.web_home.contract import ApiError, ErrorCode, NoteRecord, NoteResponse, NoteUpdate

if TYPE_CHECKING:
    from pathlib import Path

    from entrypoints.web_home.context import HomeConfig, Request

_MAX_RUN_ID = 256


def note_path(config: HomeConfig, run_id: str) -> Path:
    """Return the note file the TUI's ``notePath`` names for *run_id*."""
    if not run_id or len(run_id) > _MAX_RUN_ID:
        message = "run id must be 1 to 256 characters"
        raise ApiError(ErrorCode.INVALID_REQUEST, message)
    return config.state_home / "tui" / "notes" / f"{safe_segment(run_id)}.json"


def _read(path: Path) -> NoteRecord | None:
    try:
        return NoteRecord.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None


def get_note(request: Request) -> NoteResponse:
    """``GET /api/notes/{run}``: the note, or ``null`` when absent or unreadable."""
    return NoteResponse(note=_read(note_path(request.config, request.params[0])))


def put_note(request: Request) -> NoteResponse:
    """``PUT /api/notes/{run}``: replace the note text, keeping its creation time."""
    body = parse_body(request, NoteUpdate)
    config = request.config
    run_id = request.params[0]
    path = note_path(config, run_id)
    now = config.clock().astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with config.write_lock:
        existing = _read(path)
        note = NoteRecord(
            run_id=run_id,
            text=body.text,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        atomic_write(
            path, note.model_dump_json(by_alias=True, indent=2).encode("utf-8"), mode=0o600
        )
    return NoteResponse(note=note)
```

`app.py` final import line and route table:

```python
from entrypoints.web_home import catalog, keys, notes, projects, runs, tasks
```

```python
_PROJECT = r"/api/projects/([^/]+)"
_ROUTES: tuple[tuple[str, re.Pattern[str], Callable[[Request], BaseModel]], ...] = (
    ("GET", re.compile(r"/api/fs"), projects.list_directory),
    ("POST", re.compile(r"/api/projects/validate"), projects.validate),
    ("GET", re.compile(r"/api/projects"), projects.recent),
    ("GET", re.compile(r"/api/agents/catalog"), catalog.get_catalog),
    ("GET", re.compile(r"/api/auth"), keys.auth_status),
    ("PUT", re.compile(r"/api/auth/([^/]+)"), keys.write_key),
    ("GET", re.compile(_PROJECT + r"/tasks"), tasks.task_list),
    ("POST", re.compile(_PROJECT + r"/tasks"), tasks.create_task),
    ("GET", re.compile(_PROJECT + r"/tasks/([^/]+)"), tasks.task_detail),
    ("PUT", re.compile(_PROJECT + r"/tasks/([^/]+)"), tasks.edit_task),
    ("GET", re.compile(_PROJECT + r"/commit"), tasks.commit_preview),
    ("POST", re.compile(_PROJECT + r"/commit"), tasks.commit),
    ("GET", re.compile(_PROJECT + r"/runs"), runs.list_runs),
    ("POST", re.compile(_PROJECT + r"/runs"), runs.start_run),
    ("POST", re.compile(_PROJECT + r"/runs/([^/]+)/open"), runs.open_run),
    ("POST", re.compile(_PROJECT + r"/runs/([^/]+)/resume"), runs.resume_run),
    ("DELETE", re.compile(_PROJECT + r"/live"), runs.stop_live),
    ("GET", re.compile(r"/api/notes/([^/]+)"), notes.get_note),
    ("PUT", re.compile(r"/api/notes/([^/]+)"), notes.put_note),
)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/entrypoints/web_home -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/entrypoints/web_home tests/entrypoints/web_home/test_home_notes.py
git commit -m "feat(web): share run notes with the TUI"
```

### Task 16: Group (c) gates, end-to-end smoke, and PR

- [ ] **Step 1: Run the gates**

```bash
./scripts/format.sh
./scripts/check_format.sh
./scripts/check_lint.sh
./scripts/check_types.sh
uv run tach check
uv run python scripts/check_lint_waivers.py
uv run python scripts/check_file_length.py
uv run python scripts/check_test_isolation.py
uv run python scripts/check_doc_links.py
uv run pytest tests/entrypoints tests/server libs/vs-project/tests libs/vs-agent/tests -q --basetemp=/tmp/vsw
uv run python -m entrypoints.web_home.contract > /dev/null
```

Expected: every command exits 0.

- [ ] **Step 2: Smoke against the real run server**

With sub-project 1 merged, start `uv run python -m entrypoints.web home --port 8799 --root "$PWD/.."`, then with `TOKEN` from the printed URL and `ORIGIN=http://127.0.0.1:8799`:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Origin: $ORIGIN" -H 'Content-Type: application/json' \
  -d "{\"path\": \"$PWD/../<a ready project>\"}" http://127.0.0.1:8799/api/projects/validate
curl -s -H "Authorization: Bearer $TOKEN" -H "Origin: $ORIGIN" -H 'Content-Type: application/json' \
  -d '{"task":"<task>","outer_loop":"plain","budget":1,"compute_backend":"cpu","provider":"codex","model":"gpt-5.5","roles":{}}' \
  http://127.0.0.1:8799/api/projects/<id>/runs
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/api/projects/<id>/runs
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" -H "Origin: $ORIGIN" http://127.0.0.1:8799/api/projects/<id>/live
```

Expected: `ready`; a `LaunchResult` whose `websocket_url` accepts a WebSocket with `Origin: http://127.0.0.1:8799`; the run moves `starting` to `live`; stop returns `{"stopped": true, ...}` and the next list shows `none`. A bad `--config` model shows `launch_failed` with the stderr tail.

- [ ] **Step 3: Open the stacked PR**

`open-pr` skill, title `feat(web): run lifecycle and notes API`, base: the group (b) branch. Note the dependency on the sub-project 1 PRs in the body.

---

## Self-review

- Spec coverage: every row of the spec's endpoint table maps to a task (fs: 2; validate, projects: 3; tasks list/detail: 8; create/edit: 9; commit: 10; catalog: 4; auth GET/PUT: 5; runs list: 14; start: 12; open, resume, stop: 13; notes: 15). Auth, Origin, tokenless assets, CSP, and the stable port are Task 1. Every spec error code is in `ErrorCode` except `run_failed` (see resolutions).
- Placeholders: none; every code step carries the code that ships.
- Type consistency: handlers are `(Request) -> BaseModel`; `HomeConfig`, `Launch`, `LOOPS`, `select_task`, `resolve_project`, `inspect_project` keep one signature across tasks.
- Review Focus: each item has its test in the owning task.

## Spec ambiguities resolved

1. **API auth transport.** HTML takes `?token=` (it is the capability URL); `/api/*` takes `Authorization: Bearer` so the token never sits in API URLs or logs. Dev uses a Vite proxy with `changeOrigin: true` instead of CORS in the home server.
2. **Host check.** Added `Host` validation (DNS rebinding) on every path, including tokenless assets. Host and Origin share one allowlist derived from the home origin, so `localhost:<port>` is rejected.
3. **Where home-launched gateways publish.** `$VIBESYS_STATE_HOME/web/gateways/<project id>/live.json` and `reopen-<run>.json`, not `.vibesys/web-gateway.json`: a record or its `.lock` inside `.vibesys/` makes the tree dirty and blocks the next launch (observed while testing this plan). The default `.vibesys/web-gateway.json` is read only to report `external` and to refuse a second live run.
4. **"Live, verified by attaching and matching run identity".** The record answers `/health` with its token and has `mode == "live"`; the owner sidecar (`run_id`, `pid`, `origin`, written atomically per launch attempt) names the run and matches the record's pid, or, for an external record, the run is the project's current run; and the journal's latest attempt shows `project_attached`. Reopen records match on `(mode, run_id)`. Plan 1's live records carry no `run_id`, hence the sidecar. No WebSocket attach per list call.
5. **Ended-but-serving.** A terminal event (`run_finished`, `run_failed`, `run_interrupted`; the controller records failure as `run_failed`) in the journal's latest attempt (after the last `server_started`); a resumed run's earlier `run_finished` does not count. The live run's journal is scanned in full with a byte prefilter; history rows scan the last 256 KiB (a `ponytail:` note names the per-journal offset as the upgrade).
6. **`run_failed`.** Not an HTTP error: nothing is pending when a run fails after its gateway is ready. It surfaces as gateway state `failed` with `stderr_tail` (and `status: "failed"` for a launch not yet in the store). Launch-time failures are `launch_failed`.
7. **Runs started outside the app without `--web`.** Nothing marks them (no run lock or record), so they list as `none`; launch itself still rejects conflicts (branch exists, dirty tree).
8. **Project ids.** First 16 hex of `sha256(canonical root)`, resolved through recent projects; validating a git work tree adds it. The root is re-confined on every use, so shrinking `--root` revokes access.
9. **Validation order.** `missing, not_git, invalid, uninitialized, no_tasks, no_commits, dirty_tree, ready`, so the UI can create a task before the first commit. `invalid` is added for layout errors (for example a symlinked `.vibesys`).
10. **Commit.** Split into `GET .../commit` (preview) and `POST .../commit` (commit exactly the previewed `.vibesys/` paths). Other changes are listed, never committed.
11. **Editability.** Beyond the spec's round-trip rule, the form edits only `command` argv tasks with `[benchmark.result]`; protocol, entrypoint, and metric-less tasks are read-only. Edits apply onto the existing manifest, keeping timeouts and other sections.
12. **Key allowlist.** Profile `auth_env_vars` ending in `_API_KEY` or `_AUTH_TOKEN` (so `OPENAI_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS` are not writable). Values are written single-quoted, and `'`, `"`, `\` and `$` are rejected: python-dotenv interpolates `${VAR}` even inside single quotes, so only a `$`-free value round-trips through `dotenv_values`/`load_dotenv` unchanged (the Hypothesis test checks the round trip). An inherited variable, even empty, shadows `.env`, so shadowing is reported by membership.
13. **CLI session.** `present`/`absent`/`unknown`, never verified: the profile's primary credential file (`auth_files[0]`, relocated through `state_root_env`), else for Claude the macOS keychain item `Claude Code-credentials` checked with `security find-generic-password -s` (no `-w`, 2 s timeout; exit 44 is absent, other failures unknown).
14. **Run config.** A run-owned `<run>.agent.toml` beside the gateway record, validated as `Config` before launch. Resume passes no config: the CLI restores the recorded settings. `--run-id` is generated up front with `generate_run_id` (beside a readable `--exp-name`), so the id the reply returns is the id the run store records, known before the gateway exists.
15. **Stop.** SIGTERMs the home-launched live gateway, else the external one, as `web stop` does; it returns without waiting.
16. **Notes sanitization.** Matches the TUI per UTF-16 code unit (a non-BMP character becomes two underscores).
17. **Default compute backend.** Reported by the catalog (`metal` on Apple silicon, else `cuda`/`rocm` if their CLI exists, else `cpu`), so the setup UI does not guess.
18. **Child ownership.** Every spawned child is reaped by a daemon thread (`process.wait`), independent of UI polling. A launch that times out is terminated and reaped before `launch_failed` returns, so a retry never races it, and a record is accepted only from the pid the current attempt spawned.
19. **Port changes.** The port is saved only after a successful bind. A bind failure explains that another port changes the origin and strands gateways; after a change, stranded home-launched gateways are logged and flagged `origin_mismatch`, and `open` restarts a stranded reopen gateway.
20. **Run status.** Derived from the gateway and the journal's latest attempt, never copied from the store (the store always reports `unknown`).
21. **Known limitation: TOCTOU.** `atomic_write` refuses a symlinked target and renames a fresh `O_EXCL` temporary, but reads (`.env`, task files) follow the path at open time; a local attacker who can swap files inside the user's own checkout between check and use is out of scope. Opening reads with `O_NOFOLLOW` is the cheap upgrade if that changes.
