# VibeSys TUI — bug-hunt reference

Distilled from the TUI source (`clients/tui/src/**`) and the repo's issue
conventions. Line numbers drift; re-grep if a detail matters. Cross-check every
suspected bug against `clients/tui/README.md` and
`docs/contributing/tui-architecture.md` (intended behavior) before filing.

## Launch & environment

- Bins: `vs`, `vibesys` → `clients/tui/dist/launcher.js`. Launcher (Node) starts
  the Python backend headless over a Unix socket, then runs the frontend under
  **Bun**.
- From source: build once with `pnpm build:clients`; run
  `node clients/tui/dist/launcher.js <args>` or `uv run vibesys <args>` (a TTY is
  required for the TUI; non-TTY / `--headless` / `validate` / `-h` go headless).
- Backend Python resolves from `VIBESYS_PYTHON`, else `python3`/`python`. Set
  `VIBESYS_PYTHON=<repo>/.venv/bin/python`.
- Per-session dir: `mkdtemp(tmpdir(), 'vibesys-session-')` →
  `/tmp/vibesys-session-XXXXXX/`. Holds `control.sock` and **`backend.log`**
  (backend stdout+stderr). The dir is `rm -rf`'d on exit, so read `backend.log`
  while the run is live.
- Timeouts: readiness 30s (`READY_TIMEOUT_MS`), shutdown SIGTERM→SIGKILL 10s,
  backend-exit grace 2s. On startup failure the launcher prints the last 20
  lines of `backend.log`.

| Env var | Effect |
| --- | --- |
| `VIBESYS_TUI_RUNTIME` | frontend runtime, default `bun` |
| `VIBESYS_TUI_ENTRYPOINT` | frontend entry, default `dist/index.js` |
| `VIBESYS_CONTROL_SOCKET` | socket path (frontend throws if unset) |
| `VIBESYS_THEME` | theme name (falls back to `dark` if invalid) |
| `VIBESYS_PYTHON` | backend interpreter |
| `VIBESYS_RELEASE_SMOKE_MARKER` | release-smoke: frontend writes `renderer initialized; control protocol exchanged` to the path, then exits 0 |

## Slash commands (global command input)

| Command | Args | Effect | Surface |
| --- | --- | --- | --- |
| `/help` | — | list available + Planned commands | modal overlay |
| `/chat` | `[question]` | focus/open chat; `/chat <q>` sends now; hidden from list when chat already docked | docked pane or modal |
| `/pause` | — | `command.pause` | inline ack |
| `/resume` | — | `command.resume` | inline ack |
| `/steer` | `<message>` (req) | `command.steer {text}`; empty → `Usage: /steer <message>` | inline |
| `/open-round` | `[--N` or `N]` | no-arg drills into selected hypothesis' rounds; `--N`/`N` opens round N; bad → `Unknown round: …` | round view |
| `/perf` | — | `query.performance` → chart | split right pane (modal < 100 cols) |
| `/todos` | — | toggle todo strip (= Ctrl+T / F2) | todo strip |
| `/prompt` | — | expand latest prompt (= Ctrl+P / F3) | inline |
| `/theme` | `[name]` | no-arg picker; `<name>` switches; unknown → `Unknown theme: …. Available: …` | modal picker |

Parse errors: unknown `/x` → `Unknown command: /x. Use /help.`; empty →
`Enter a slash command. Use /help.`; non-slash → `Commands start with /. Use
Experiment chat for questions.` **Planned (not accepted):** `/round <n>`,
`/invocation <id>`.

Chat-composer commands (resolved before global forwarding): `/clear` (new
thread), `/model` (harness+model menu), `/resume` (shadows global: thread-switch
list). Other global slash commands typed in chat are forwarded.

## Keybindings (first match wins)

| Key / chord | Action | Context |
| --- | --- | --- |
| `Ctrl+C` | copy OSC52 selection; **no selection → exit** | always (highest) |
| `F4` | zoom focused pane | chat closed, no overlay/picker |
| `Ctrl+PgUp`/`Ctrl+PgDn` | scroll error banner | banner present |
| `Escape` | dismiss error banner | banner present |
| `Ctrl+W` | cycle pane focus | a right/chat pane visible |
| `PgUp`/`PgDn` | scroll focused pane | right/chat focused |
| `Esc` | close pane / focus left | right/chat focused |
| `Up`/`Down` | theme picker move ±1; chat suggestion nav | picker / chat |
| `Enter` | apply theme (input empty); confirm chat menu | picker / chat menu |
| `Ctrl+L` | return to experiment log (live, scroll bottom) | global |
| experiment log `Up`/`Down` | move selection (or round-select in detail) | log |
| experiment log `Enter` | run typed command, else open selected hypothesis | log, input empty |
| `Esc` (log) | leave hypothesis detail | detail open |
| `Ctrl+P`/`F3` | toggle latest prompt | round view |
| `Ctrl+T`/`F2` | toggle todos; then `Up`/`Down` select, `Esc` collapse | round view |
| `Left`/`Right` | focus agents / transcript | round view |
| `Up`/`Down` (round) | prev/next agent, or select transcript entry | round view |
| `Enter` (round) | toggle selected tool card | transcript focused, input empty |
| `Tab` / `Shift+Tab` | complete suggestion, else next/prev agent | global |
| `[` / `]` | prev / next round | global |
| `Esc` (round scope) | clear entry sel → agent sel → leave drilldown | in a hypothesis |
| `Home`/`End`, `Ctrl+Up`/`Ctrl+Down` | scroll top/bottom, by line | fallthrough |
| mouse click | focus pane; row→hypothesis; round chip→trajectory; agent node→filter; card→expand | per view |

Chat composer: `Enter` submit, `Shift+Enter` newline.

## Responsive breakpoints (exact)

- **Split (transcript + right/perf):** `MIN_SPLIT_WIDTH = 100`. Below 100 cols a
  `/perf` viz falls back to a modal. Right pane width `round(w*0.45)` clamped
  `[62, 84]`, then `min(., w-38)`.
- **Chat dock vs modal:** docks when available ≥ **92 cols**
  (`LOG_COMPACT_PANEL_WIDTH 67 + CHAT_PANE_MIN 25`); below that a question opens
  chat as an 80%×76% modal. Docked width `clamp(available-95, 25, 52)`.
- **Experiment-log columns** (bodyWidth = availableWidth − 5): always keep
  Hypothesis(15)+Rounds(8)+Outcome(11). Add `Measured` at ≥62, `Claim/Impl
  Details` at ≥90, `Kept` at ≥104. **Drop widest-first: Kept(104) → Claim(90) →
  Measured(62).** Footer hint collapses below 60.
- **Agents pane:** left-to-right graph when it fits, else a stacked vertical
  list with `↓` connectors (`STACKED_WIDTH 30`, transcript floor 42). Agents
  pane is hidden first when a split pane opens.
- **Fixed sizes:** error banner 10 rows; overlay 70%×60% at 15%/18%; chat modal
  80%×76% at 10%/10%; perf chart 8×48.
- **Zoom (F4):** gives one pane the whole content row; overrides split/dock.

## Error surfaces

- **Error banner** (10 rows, top-rooted): title line
  `<title> · <agentKind> · <roundLabel> · <count> reports`, then wrapped
  `message`, `Detail:`, `Hint:`, footer `[× Dismiss] · Esc: dismiss ·
  Ctrl+PgUp/PgDn: scroll`. Border `error` if fatal else `warning`. Scopes:
  configuration/invocation/phase/run/protocol/request/transport/input. Titles
  e.g. `protocol→Protocol error`, `transport→Connection lost`,
  `request→Request failed`, `input→Input error`, `run→Run failed`. Equivalent
  reports fold into one banner and bump `count`.
- **Modal overlay** (`detail`/`help`/`error`): titles Command/Help/Error;
  footer `Esc to close · PgUp/PgDn: scroll` on its own reserved row, over a
  scroll viewport that PgUp/PgDn page while the overlay is open and that starts
  at the top on every open. Used for acks, `/help`, perf modal fallback.
- **argparse diagnostics** print to the terminal for `-h`/`validate` (bypass
  TUI). In server mode a backend argparse failure is surfaced as an error-banner
  diagnostic (stage `argument_parsing`, exit code, hint) rather than exiting.
- **Raw errors:** `<sessionDir>/backend.log`. Control socket is line-delimited
  JSON; readiness uses `query.snapshot` expecting `{ok:true}`.

## Fragile areas (target these)

1. Streaming markdown tail (`streaming` flag; append-suffix vs single-entry vs
   full rebuild fast paths). Boundary bugs when the tail grows vs one entry
   mutates, or identity/order shifts.
2. Parallel tool-call cards keyed by call ID; expansion state by entry id.
   Mis-keying when a streamed result and its call lack stable shared ids.
3. Filter / history-window rebuilds (agent filter, hypothesis scope). Selection
   held by hypothesis id across refresh. `visibleActiveExecutions` returns [] on
   transport loss (active work vanishes).
4. OSC52 clipboard: no selection → exit; unsupported → keep selection, different
   status line. Ctrl+C double role (copy vs quit) is the sharp edge.
5. Resize reflow: widths derived live; panes cache rendered width/state and skip
   redraw when unchanged (a width-only change must still redraw). Chat draft
   parked/restored per thread and shared between docked/modal composers.
6. Theme contrast derivation (`ensureContrast` mixes toward black/white until
   `minContrast`). Live switch rebuilds markdown style; preview vs applied and
   style disposal timing are delicate.
7. Agent graph edge routing (lane allocation, junction glyphs, arrowheads,
   tone-rank merge). Multi-agent-per-kind columns are written-for but untested.
8. 1s elapsed-time timers in experiment-log and agent-map; leak risk on
   off-path teardown.

## Stub vs real agent

Bug hunting always uses the **real** Claude provider (project preference); stub
is documented here only for context and is not used for finding bugs.

- `--stub-agent`: deterministic canned responses, no LLM, ~50ms/call. Cycles 3
  hypothesis claims, 2 rounds each, every 3rd disproven; rising metric
  `1000 + 45*index` (−20 on regressed), unit `median_tok_per_sec`; one measured
  number per hypothesis. Chat returns a fixed string. Use it to isolate frontend
  bugs (layout, streaming, selection, columns, perf chart, agent graph, error
  banners) from LLM nondeterminism, at zero token cost.
- Real (`--cli-provider claude`): exercises the live backend event stream, real
  agent turns, experiment chat wired to a coding agent, real measured metrics,
  pauses/steers, and long-run behavior. Use it for backend/protocol/chat/perf
  bugs and enhancement ideas.

## Reporting: labels, forms, conventions

- **No `tui`/`frontend`/`ui` label exists.** TUI issues are labeled `bug` or
  `enhancement` and parented under roadmap **#284 "Improve VibeSys TUI"**. Do
  not invent a TUI label; do not clone/modify #284.
- **Bug → form `.github/ISSUE_TEMPLATE/01-bug.yml`.** Required sections, in
  order: Observed behavior; Expected behavior; Reproduction (numbered);
  **Affected subsystem** (dropdown, no TUI option → pick `CLI and packaging` or
  `Unsure`); Environment (add terminal emulator + **terminal size** + **theme** +
  commit + OS); **Impact** (`Blocks normal use` / `Major functionality is
  impaired` / `Workaround exists` / `Minor or cosmetic`); Relevant logs
  (optional, `render: shell`); Related issues; Pre-submission checks.
- **Enhancement / feature / refactor → form `02-engineering-change.yml`.**
  Sections: **Workstream** (dropdown → `CLI/DX` for TUI); Problem; Desired
  outcome; Acceptance criteria (checkboxes); Scope and non-goals; Constraints;
  Verification approach; Parent (`#284`).
- **Titles:** specific, outcome-oriented; no `[Bug]`/`[Feature]` type prefix; a
  topic prefix `TUI:` is allowed. One issue = one independently closable
  outcome. Do not set Priority/Effort/assignee/milestone.
- **Before filing:** search open + closed issues (dedupe/superseded), inspect
  code + merged PRs to confirm it isn't already done, redact secrets. Use the
  repo-local `create-issue` skill.

## Real issues to model against (by category)

- **Display/render:** #418 (streaming chunks per-line), #427 (composer hides
  long input), #440 (banner not dismissable), #256 (typed input not echoed),
  #255 (rounds strip hides >8 rounds), #410 (theme not honored on launch).
- **Interaction/glitches:** #433 (focus indicator inconsistent), #399 (arrow
  keys don't move suggestion), #400 (no theme live-preview), #331 (picker keys
  leak to view), #442 (non-slash text sent to chat), #259 (agent tabs not
  clickable), #429 (Ctrl+C exits instead of copy), #420 (resumed hypothesis
  shows empty), #425 (chat fails after Omnigent, event-loop reentry), #276
  (stale invalid-owner error).
- **Backend/protocol affecting TUI:** #444 (package boundaries
  transport←core-state←tui), #282 (chat wired to real agent), #329 (stub
  launch time), #267 (Bun worker EACCES in CI).
- **Features/UX:** #194 (multi-turn chat), #260 (themes), #283 (split-pane viz),
  #285 (hypothesis-log landing), #396 (agent graph), #432 (F4 zoom).
