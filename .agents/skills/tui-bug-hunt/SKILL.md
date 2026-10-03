---
name: tui-bug-hunt
description: >-
  Drive the VibeSys terminal UI (TUI) headlessly via tmux against real
  Claude-Code-provider runs, to find and report display bugs,
  frontend/interaction glitches, backend/protocol problems, and UX/feature gaps,
  then turn findings into GitHub-issue-ready reports. Use when asked to test,
  QA, exercise, stress, or "find bugs in" the VibeSys TUI / client / launcher /
  experiment log / chat / rounds view / agent graph / perf pane / themes, or to
  produce TUI issue tickets. Covers responsive-layout, keybinding, streaming,
  error-banner, clipboard, and theme testing.
---

# VibeSys TUI bug hunt

Goal: exercise the VibeSys TUI end to end, catch **all types of frontend and
backend bugs** (display/layout, interaction/keybinding, streaming, theme/contrast
on the frontend; protocol, lifecycle, state, and evaluator/benchmark on the
backend), and produce reports that map cleanly onto the repo's issue forms.
Enhancements and **net-new features are in scope to identify and report**: when
a surface is missing something it should have, file it as an engineering change
(§7). The TUI is a full-screen OpenTUI app, so it is driven through a real PTY
provided by **tmux**; no human at a terminal is needed.

**Priority order (do not reorder):** (1) find the issue, (2) help the user open a
GitHub issue ticket for it, (3) only then, and only with the user's explicit
approval, discuss and implement a fix or feature. **Never write code / open a PR
for a finding before the ticket exists and the user has approved implementation.**
This skill's job ends at a filed ticket unless the user says to go further. The
fixing guidance in §8 applies only once that approval exists.

Always run a real Claude-Code-provider run, and keep it to **1-2 rounds** (§2):
short enough to be cheap, long enough to reach a resolved hypothesis and see the
log table, `Measured` deltas, and `/perf` update live.

Read `reference.md` (same folder) for the exhaustive command/keybinding/
breakpoint/error/issue-form tables. This file is the workflow.

## 0. Prerequisites (verify once)

- Toolchains on PATH: `node` (≥20), `bun`, `pnpm`, `tmux`, and `uv`; plus `go` +
  `cargo` when running real rounds on the queue example. `claude` CLI installed
  and authenticated (`claude -p "ok"` returns `ok`) for real-provider runs.
- TUI built: `clients/tui/dist/launcher.js` and `dist/index.js` exist. If not:
  `pnpm install --frozen-lockfile && pnpm --dir clients/backend-client generate:protocol && pnpm build:clients`.
- Python env: `<repo>/.venv` with `vibesys` installed (`uv sync`). Export
  `VIBESYS_PYTHON=<repo>/.venv/bin/python`.
- A standalone candidate repo (its own git root). The bundled `queue-rs` example
  is nested in this repo, so copy it out first, e.g.
  `/tmp/vibesys-runs/queue-{spsc,mpsc,mpmc}`.
- On a shared host, the framework benchmark path collision (hardcoded
  `/tmp/vibesys-framework-benchmark-*.json`) can block benchmark recording; the
  per-uid fix in `loop.py` resolves it. If `/perf` is empty, check
  `backend.log` for `framework-benchmark FAIL: ... permission denied`.

## 1. The driving harness

Use `tuidrv.sh` in this folder. It wraps a detached tmux session.

```
tuidrv.sh start <COLSxROWS> <PROJECT> <TASK> [vibesys args...]
tuidrv.sh cap        # rendered frame, plain text (layout/wrapping/alignment)
tuidrv.sh capE       # frame WITH ansi escapes (color / theme / contrast checks)
tuidrv.sh keys <k…>  # named keys: Enter Escape C-w C-l F4 F2 F3 Up Down Tab BTab '[' ']' PgUp PgDn
tuidrv.sh type <txt> # literal text
tuidrv.sh cmd <txt>  # type then Enter (e.g. cmd /help)
tuidrv.sh size <WxH> # resize to test responsive layout
tuidrv.sh log [N]    # tail the live backend.log
tuidrv.sh wait [S]   # let renders / agent turns settle
tuidrv.sh stop
```

Driving discipline:
- After every `start`, `wait 15-20` (backend readiness up to 30s), then `cap`.
- After every keystroke/command, `wait 1-2` before `cap`; agent turns need
  longer (`wait 10-30`) in real mode.
- Judge alignment from `cap` **without stripping trailing spaces** (borders live
  at the right edge). Judge color/contrast from `capE`.
- Always `cap` the frame into your notes verbatim as evidence before acting.
- One session at a time (default name `vsbug`); `stop` when done or before a new
  `start`. Set `VS_TUI_SESSION` to run several.

## 2. Run mode: always real (Claude Code provider)

Always hunt against a real run. **Do NOT use `--stub-agent`** for bug hunting:
the stub is a mock and misses real backend/stream/agent behavior, and this
project requires findings to come from reality.

```
start 200x50 /tmp/vibesys-runs/queue-mpmc mpmc --profiler none --cli-provider claude --max-rounds 2
```

This uses the Claude Code provider and exercises the live event stream, real
agent turns, chat wired to a coding agent, real measured metrics (`/perf`), and
`/pause` `/steer` `/resume`. It is slower and spends tokens, so **default to
`--max-rounds 2` (1 is fine for a quick pass, never more than 2 unless the user
asks for a long run)**, and reuse the one running session for the entire sweep
rather than relaunching. Two rounds is enough to see a hypothesis resolve, the
log table refetch, `Measured` deltas populate, and `/perf` plot a second point.

Before each session, clean prior state (stale tmux sessions, leftover
`/tmp/vibesys-session-*`, and prior run state) and re-stage a pristine project
copy, so a run never fails on a resumed/duplicated hypothesis or a `/tmp`
collision. The framework benchmark path is now per-uid, so cross-user `/tmp`
collisions no longer block benchmark recording; if `/perf` is still empty, check
`backend.log` for `framework-benchmark FAIL`.

Freeze for inspection: live updates race your captures. Use `/pause` (or wait
for a round boundary) before the responsive-size, theme, and keybinding passes,
so a layout change is attributable to your resize/keys and not to new events
landing. `/resume` to continue.

## 3. Systematic sweep protocol

Work the surfaces in order. At each step: send the keys/command, `wait`, `cap`
(and `capE` when color matters), and check against the intended behavior in
`reference.md` / the TUI README. Log every deviation as a candidate finding.

1. **Startup & experiment log.** Launch real (claude provider); wait for the
   backend (≤30s) and the first agent turn. Verify the landing view is the
   hypothesis-keyed log: columns present for the width, `▸` on the active
   hypothesis, spelled-out `Accepted`/`Rejected` (not color-only), rounds ranges,
   `Measured` deltas. Check the footer hint and the `Ctrl+W to type here` chat
   instruction.
2. **Log navigation.** `Up`/`Down` move selection; `PgUp`/`PgDn` scroll; `Enter`
   opens the selected hypothesis. Confirm selection survives a table refetch
   (it is keyed by hypothesis id).
3. **Hypothesis summary → round.** In a hypothesis, select a round, `Enter` to
   open the transcript + rounds strip + agent map. `[`/`]` change rounds;
   `Tab`/`Shift+Tab` change agents; arrows move the transcript cursor; `Enter`
   toggles a tool card; `F2`/`Ctrl+T` todos; `F3`/`Ctrl+P` latest prompt.
   `Escape` should unwind entry cursor → agent filter → hypothesis.
4. **Agent strip/graph.** Confirm the graph renders at wide widths and falls
   back to the stacked list when narrow; clicking a node filters the transcript;
   clicking the selected node clears the filter (mouse via tmux is optional).
5. **Commands.** Run each: `/help`, `/theme`, `/theme <name>`, `/perf`,
   `/todos`, `/prompt`, `/pause`, `/resume`, `/steer x` (and empty `/steer`),
   `/open-round`, `/open-round --2`. Verify each surface (modal vs pane vs
   inline) and the exact error strings for bad input. Confirm `/help` lists the
   right commands and the `Planned` section.
6. **Chat.** `Ctrl+W` moves focus to the chat and back (focus border + `▸`
   move). Ask a question; in stub it returns a fixed string. Test `/clear`,
   `/model`, and a slash command typed in chat (forwarded). Dock vs modal is
   width-driven (see step 8).
7. **Split panes & zoom.** `/perf` opens a right pane (≥100 cols) or a modal
   (<100). `Ctrl+W` cycles focus across visible panes; `F4` zooms the focused
   pane and restores selection/scroll on toggle; `Escape` on the right pane
   closes it.
8. **Responsive matrix.** Repeat key views at several sizes and check reflow and
   the exact breakpoints in `reference.md`: `size 200x50` (wide), `120x40`,
   `104x40`, `100x40`, `92x40`, `88x40`, `72x30`, `60x24`. Watch: chat docks ≥92
   else modal; `/perf` splits ≥100 else modal; log columns drop Kept(104) →
   Claim(90) → Measured(62); agents graph → stacked; footer hint collapses <60.
   Resize both directions; a width-only change must still redraw (cached-width
   bug risk).
9. **Themes.** For each theme (`dark`, `light`, `solarized-dark/light`,
   `catppuccin-mocha/latte`, `high-contrast-dark/light`): `/theme <name>` then
   `capE`. Check contrast of muted/subtle text and that status meaning does not
   depend on color (glyph + word present). Also test `--theme` flag and
   `agent.toml [tui].theme` precedence, and the picker (`/theme`, then `Up`/
   `Down`, `Enter`, `Esc`).
10. **Error surfaces.** Trigger the banner and confirm it is dismissable
    (`Escape`, `Ctrl+PgUp/PgDn` scroll) and does not permanently eat 10 rows.
    Trigger a backend argparse diagnostic by adding a bogus flag (e.g.
    `--nope`) to `start`; confirm it surfaces as a banner with stage/exit/hint,
    not a crash. Open `/help` while a banner is up and check for border overlap.
11. **Clipboard / Ctrl+C.** With no selection, `Ctrl+C` exits (expected). Note
    OSC52 selection behavior and the status-line difference when unsupported.
12. **Backend & lifecycle.** Throughout, `tuidrv.sh log` for tracebacks,
    protocol errors, timeouts, or `permission denied`. In real mode, let a run
    reach a resolved hypothesis and confirm the log table and `Measured`/`/perf`
    update live (table refetches on phase/round completion). Test `/pause` then
    `/resume`. Kill the backend mid-run (`stop`) and, on a fresh `start
    --resume`, confirm resumed hypotheses are not shown empty.

## 4. Configuration & feature matrix to exercise

The sweep above walks one config. Most real bugs hide in **combinations of state
you have to deliberately construct**: a specific size crossed with a specific
theme, a resumed run, a mid-stream resize, an error banner plus an overlay.
Vary one axis at a time from a known-good baseline so a regression is
attributable. Cross the high-value axes below; capture the frame at each cell.

- **Terminal geometry.** Every breakpoint in `reference.md` §Responsive, plus the
  boundary itself (`99` vs `100`, `91` vs `92`, `103` vs `104`, `61` vs `62`) and
  one column *below* the smallest (`58x20`) to see graceful degradation vs
  corruption. Also very tall/short (`120x12`, `120x80`) to stress vertical
  reservation (banner 10 rows, perf chart 8, overlay 60%). Resize **while a turn
  is streaming** and **while a modal/picker is open**.
- **Themes × geometry.** Each theme at wide and at the narrowest usable size;
  a live `/theme` switch *while* a modal, the perf pane, and the error banner
  are open (style-disposal and preview-vs-applied bugs live here). Precedence:
  `--theme` flag vs `agent.toml [tui].theme` vs `VIBESYS_THEME` vs default.
- **Launch flags.** `--max-rounds 1|2`, `--profiler none` vs a real profiler,
  `--theme`, `--resume`, a bogus flag (`--nope`) for the argparse banner, and
  `--headless`/`-h`/`validate` (must bypass the TUI, not half-render it).
- **`agent.toml` config.** `[tui].theme`, model/provider blocks, and any
  `[tui]` keys. Feed an **invalid** value (bad theme name, unknown key) and
  confirm it fails with a named error, not a silent fallback or a crash
  (AGENTS.md: reject unknown keys).
- **Providers / models.** Default claude provider is the baseline. If asked to go
  broader, `--cli-provider codex` with a `gpt-*` model exercises a different
  turn/stream shape; `/model` in chat switches harness+model mid-run.
- **Lifecycle transitions.** `/pause`→resize→`/resume`; kill backend mid-round
  then `--resume` (resumed hypotheses must not render empty, per #420); let a
  hypothesis resolve `Accepted`, `Rejected`, and `Deferred`/unmeasured and check
  each renders with a word+glyph, not color alone. Steer mid-run (`/steer …`)
  and confirm the injected guidance appears in the next turn.
- **Data-shape stress.** A run with >8 rounds (rounds-strip overflow, #255); a
  long hypothesis claim / long chat input (composer clipping, #427); parallel
  tool calls in one turn (card keying); a turn that errors (banner scope/title).
- **Focus & overlay stacking.** Every pair of {chat docked, chat modal, perf
  pane, perf modal, `/help`, `/theme` picker, error banner, todo strip, F4 zoom}
  open together. Keys must reach the top surface only; nothing should leak to the
  view behind a modal/picker (#331), and overlays must not collide (§10 seed).

Not every cell is worth a capture on every run. Prioritize boundaries,
transitions, and stacked surfaces; those are where the contract is thinnest.

## 5. Detection heuristics & adversarial mindset

Hunt like an adversary: for each surface ask "what state would make this
formatting, keying, caching, or reservation assumption wrong?", then construct
it. The richest bugs come from a surface built for the common case meeting an
edge you forced (empty, overflow, mutation-in-place, transport loss, a width the
author cached).

A finding is any deviation from `reference.md` / the README, or any of:

- **Layout:** broken/misaligned box-drawing borders; content past the right
  edge; overlapping panes/overlays (e.g. a modal colliding with the error
  banner); truncation without an ellipsis or a `‹ n` / `n ›` more-indicator;
  a column that should have dropped (or shouldn't have) at a given width; empty
  region where data is expected.
- **Text/stream:** one-token-per-line breaks; duplicated or dropped streamed
  tail; a mutated entry not updating in place; stale content after a filter or
  round change.
- **Color/theme (`capE`):** low-contrast muted/subtle text; status conveyed by
  color only (no glyph/word); wrong theme after launch or a leak of the previous
  theme's colors after a switch.
- **Interaction:** a key that moves the wrong thing (e.g. arrows scroll the view
  instead of a suggestion/menu); focus indicator missing or wrong; a click that
  does nothing; keys leaking to the view behind a modal/picker; a suggestion Tab
  that always completes the first match.
- **Backend/protocol (log):** Python traceback, `protocol_error`, transport
  timeout, `permission denied`, `event loop is already running`, or a
  `ValueError`/state error that fails the run.
- **Lifecycle:** selection/scroll lost on refetch or resize; timers/leaks;
  resumed state shown empty; run that fails where the stub scenario should
  succeed.
- **Contract disagreement:** two surfaces that derive the same fact disagree
  (e.g. a streamed event says `skipped` while the persisted/projected record says
  `fail`). When two views of one datum diverge, one of them is reading a stale or
  mis-owned field; that is a backend/projection bug even though you saw it in the
  UI. Note both values and where each is computed.

For each, decide **bug vs intended**: some behaviors are deliberate contracts
(e.g. non-slash text in the command box is intentionally not accepted; the chat
composer owns questions; the frontend must not optimistically mutate
backend-authoritative state). Cite the README/architecture line the behavior
matches or violates before filing. When unsure whether the fault is render vs
state vs backend, that judgement belongs in reproduction (§6), not in the ticket
title.

## 6. Reproducing & isolating a finding

A finding is only worth filing once you can make it happen on demand and can say
which layer owns it. Turn each candidate into a **minimal, deterministic,
re-runnable** repro before writing it up.

1. **Reproduce from a clean start.** Re-stage a pristine project, `start` fresh,
   and replay the exact `tuidrv.sh` sequence (size, project/task, flags, keys,
   commands) that produced it. If it does not recur, it was a race or leftover
   state; keep driving until you either reproduce it deterministically or can
   describe the timing that triggers it.
2. **Minimize.** Drop every step that is not required. Shrink `--max-rounds`,
   remove intermediate keys, and find the smallest terminal size and simplest
   theme that still shows it. The goal is a numbered list a maintainer can paste.
3. **Bisect the trigger axis.** Layout/reflow bugs: binary-search the width
   around the failing size to pin the exact breakpoint column. Theme bugs: find
   the minimal theme set. Timing bugs: `/pause` at the boundary and check whether
   the artifact is present frozen (render/state bug) or only appears while events
   land (stream/lifecycle bug).
4. **Locate the layer (this decides where the fix lives, §8).** Use `capE` and
   `backend.log` together:
   - Same wrong pixels when frozen with `/pause`, no log anomaly → **render/UI**
     state (`clients/tui/src/ui/**` or `session-model`/`session-controller`).
   - Wrong data present but formatted correctly (a count, a status, a selection
     surviving refetch) → **core-state** fold or a **backend projection**
     (`src/vibesys/server/**`), not rendering.
   - Traceback / `protocol_error` / malformed frame in `backend.log`, or the
     event stream and the persisted record disagree → **backend-client framing**
     or **Python backend/loop** (`src/vibesys/loops/**`, `server/**`).
   State which one your evidence supports; the ticket's "Affected subsystem" and
   the eventual fix both hinge on it.
5. **Capture evidence verbatim.** Paste the `cap`/`capE` frame (trailing spaces
   intact) and the relevant `backend.log` lines. Record the environment the form
   asks for: terminal size, theme, commit, OS, provider mode.
6. **Confirm it is not already fixed.** Re-check against current `main` and
   search open+closed issues and merged PRs before filing (§7).

## 7. Reporting (issue-ready)

Produce one report per independently closable finding. Use the repo forms (see
`reference.md` §Reporting) and the repo-local `create-issue` skill.

- **Bug →** `01-bug.yml`: Observed; Expected (cite intended behavior); numbered
  Reproduction (exact `tuidrv.sh` sequence: size, project/task, keys/commands);
  Affected subsystem = `CLI and packaging` (or `Unsure`); Environment
  (**terminal size + theme** + commit + OS + provider mode); Impact; Relevant
  logs (paste the `cap`/`capE` frame and any `backend.log` lines); Related
  issues; checks. Label `bug`, parent `#284`.
- **Enhancement/feature/refactor →** `02-engineering-change.yml`: Workstream
  `CLI/DX`; Problem; Desired outcome; Acceptance criteria; Verification; Parent
  `#284`. Label `enhancement`.
- Title: specific outcome, no `[Bug]`/`[Feature]` prefix (topic prefix `TUI:` is
  fine). Search open+closed issues first to avoid dupes; the taxonomy list in
  `reference.md` is the known landscape.
- Rank findings by severity (Blocks use > Major impaired > Workaround exists >
  Minor/cosmetic) and present the list to the user for which to file. Do not
  create GitHub issues without the user's go-ahead.
- Stop at the ticket. Implementation is a **separate, later step that requires
  the user's explicit approval** (see the priority order at the top and §8). Do
  not write code, edit files, or open a PR for a finding until the ticket exists
  and the user has said to implement it.

## 8. From finding to fix (only after the user approves implementation)

When, and only when, the user has approved a fix for a filed ticket, implement it
the way the repo expects: small, root-caused, in the owning layer, with a
regression test and the relevant checks green. Read
`AGENTS.md`, the `software-design` and `testing` skills, and
`docs/contributing/tui-architecture.md` first; the points below are the parts
that bite TUI fixes most.

**Fix the cause, not the frame.** The reproduction already told you the layer
(§6.4). Trace the wrong value back to where it is *produced*, not where it is
*displayed*. A `resolution=rejected` shown on an active hypothesis is authored in
the loop/projection, not in the row renderer; patching the renderer would hide
one symptom and leave the record corrupt. Confirm the root cause explains every
symptom you observed before changing anything.

**Respect the layer boundaries (they are enforced).** The TypeScript frontend is
three packages with one legal dependency direction, checked by
`pnpm check:ts-architecture`:

```
@vibesys/backend-client  <-  @vibesys/core-state  <-  @vibesys/tui
```

Ownership (put the fix where the state lives, per `tui-architecture.md`):

| Symptom | Owner | Where |
| --- | --- | --- |
| Protocol types, socket framing, connection lifecycle, requests, event subscription | `backend-client` | `clients/backend-client/**` |
| Status, rounds, phases, executions, transcripts, todos, usage, benchmarks, diagnostics (a pure fold over snapshot + ordered events) | `core-state` | `clients/core-state/**` |
| Focus, selection, layout, zoom, theme, modals, drafts, query progress; widgets, rendering, key/mouse | `tui` | `clients/tui/src/**`, `ui/**` |
| Event/query payloads, run state, projection into experiments/design/rounds, the optimization loop | Python backend | `src/vibesys/server/**`, `src/vibesys/loops/**` |

Rules that follow from this and from AGENTS.md and the `software-design` skill:
- **Backend events are the frontend contract.** Emit semantic fields from the
  backend; do formatting, truncation, color, and layout in the consumer. Do not
  move presentation into `server/**`, and do not teach `core-state` about themes,
  layout, or OpenTUI (it has no Node/renderer dependency).
- **Unidirectional flow.** A frontend action may send a command, but must not
  optimistically mutate backend-authoritative state; the resulting backend event
  does. Do not add a reverse or cross-package relative import to reach a value;
  thread it through the owning package's public entry point.
- **Fold vs render.** A selection-survives-refetch, count, or status bug belongs
  in the `core-state` fold (keyed by id, clock passed in explicitly), not in a
  widget. A misaligned border, wrong breakpoint, or focus-indicator bug belongs
  in `clients/tui/src/ui/**`.

**Contract changes are authoritative-first.** If the fix needs a new or changed
event/query field, edit the one authoritative definition
(`src/vibesys/server/protocol.py`), then regenerate the TS bindings
(`pnpm --dir clients/backend-client generate:protocol`) rather than hand-editing
generated files. Keep changes additive/backward-compatible; bump the protocol
version only for an incompatible change. Round-trip a representative payload at
the boundary in a test. The committed-schema drift test must stay green (a
no-wire-change fix regenerates to a zero diff, as PR #495/#497 note).

**Keep the change small and idiomatic.** Match the surrounding file's naming,
comment density, and idiom. Narrow the diff to the requested behavior. Do not
opportunistically reformat or rename in the same change. If you discover a real
refactor is warranted (duplication across widgets, a missing ownership boundary,
a value computed in the wrong layer), do it **only when it removes real
duplication or clarifies data flow**, keep it in a separate commit from the bug
fix, and never let it cross a package boundary the architecture check forbids.
Prefer extending the canonical module over adding a compatibility shim.

**Add a regression test at the layer that owned the bug.** core-state fold test
for a projection/selection bug; a `*.test.ts` render/app/controller test in
`clients/tui/src/**` for a UI bug; a Python `tests/**` test for a
backend/loop/projection bug. The test should fail on the unfixed code and pass
after (as issue #503's did). Reuse an existing test that already reaches the
corrupted state and add the missing assertion when one exists.

**Run the smallest relevant checks before handing back.**
- Frontend: `pnpm check:ts-architecture`, `pnpm check:clients`,
  `pnpm test:clients`, `pnpm build:clients`; if the protocol changed,
  regenerate and confirm zero unexpected drift.
- Backend: the targeted `pytest` module(s) plus `ruff check` and `ty check` on
  the changed files.
- Re-drive the original `tuidrv.sh` reproduction end to end and confirm the
  finding is gone and nothing adjacent regressed.

**Open the PR with the repo template.** Fill Problem, Solution (with an
`### Architecture` mermaid diagram and an ownership paragraph, matching recent
TUI PRs), and Verification (Correctness properties + Testing). Link the ticket
(`Fixes #NNN`) and follow `.github/pull_request_template.md`.

## 9. Coverage log

Track what was exercised so gaps are visible. Minimum columns: surface, size,
theme, mode (stub/real), result (ok / finding-id), evidence (frame excerpt or
`backend.log` line). Report the matrix plus a ranked findings list at the end.

## 10. Seed findings (already observed here; verify + expand)

Two candidates surfaced while building this skill; re-verify with the protocol
before filing, and use them as worked examples of the format:

- **Stub path is broken (bug found, not our hunting mode):** `--stub-agent` on a
  pristine `queue-spsc` fails headless with `ValueError: hypothesis ID 'H-01'
  was already used` (the stub reuses H-01). We hunt with the real provider, but
  this is a genuine issue worth filing against the stub/loop path.
- **Overlay collides with error banner:** with the `Run failed` banner present,
  `/help` opens its modal overlapping the banner's footer row
  (`[× Dismiss] · Esc: dismiss ·` is overwritten by the Help box border). Likely
  a z-order/reserved-rows display bug. Confirm the banner remains dismissable
  underneath and whether other overlays (`/theme`, `/perf` modal) collide too.
