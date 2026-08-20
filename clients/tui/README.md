# VibeSys TUI

Terminal client and launcher for VibeSys.

```bash
npm install -g @vibesys/tui
vs --help
```

The package installs `vs` and `vibesys` as aliases for the same launcher. The
launcher starts the Python VibeSys backend with `python -m vibesys --headless`
from the current directory and then attaches the OpenTUI client. The launcher
passes run arguments through unchanged. With no `--input`, the current
directory is the in-place project; pass `--input PATH` to select another
complete project, or `--runs-dir PATH` to use an experiment collection. Install
the Python `vibesys` package in the Python environment you want to use, or set
`VIBESYS_PYTHON` to that Python executable.

## Operator interface

Enter ordinary text to ask the supervision backend about the current run. The
available slash commands are:

| Command | Behavior |
| --- | --- |
| `/help` | Show commands and planned controls. |
| `/chat` | Put the pane keys on the docked chat, or open it as a modal where it cannot dock; `/chat <question>` asks immediately. |
| | Slash commands work inside the chat too, and do the same thing as in the main input. |
| `/pause` | Pause after the current agent call finishes. |
| `/resume` | Resume a paused run. |
| `/steer <message>` | Queue an instruction that is appended to the next agent invocation's prompt. |
| `/open-round` | Open the rounds behind the selected hypothesis. |
| `/open-round --N` | Open round N, inside whichever hypothesis owns it. |
| `/perf` | Plot the recorded performance metric by round, in the right pane. |
| `/theme` | Pick a theme from a keyboard-navigable list; `/theme <name>` switches immediately. |

### Experiment log

The client opens on the experiment log rather than on the per-round
transcript. It groups rounds by `hypothesis_id`, so one hypothesis held across
continuation rounds is a single row showing the claim, the round range, what
the implementation details, the measured result, the judge verdict, the outcome the loop
resolved (`Proven`, `Rejected`, or a terminal `HypothesisOutcome`), and whether
the candidate was kept. The active hypothesis is marked with `▸` and carries no
outcome until it resolves. `Proven` reads in the theme's success color and
`Disproven` in its error color; the word is always spelled out, so the reading
does not depend on color.

The log is the landing view and the root of the client, so no command is
needed to reach it: it is what the client opens on, and Escape or Ctrl+L
returns to it from anywhere.

Arrow keys move the selection, the wheel and trackpad scroll the table
independently of it, and clicking a row selects it. Enter on an empty input, or
`/open-round`, opens the rounds behind the selected hypothesis: the ordinary
transcript, rounds strip, and agent map, filtered to that hypothesis. The input
keeps Enter whenever something is typed, so a command entered from the log runs
on its first Enter and its result opens over the table. `/open-round --N`
jumps straight to round N inside whichever hypothesis owns it. Escape steps
back to the table with the selection intact. The log is the root view, so
opening a hypothesis is the only route to per-round output; there is no
unfiltered live transcript to fall back to.

The agent strip is headed `Round N flow · 45s` for the round on screen. That
elapsed time is agent-active: wall clock minus the gaps where no agent was
running, the same measure the rounds strip reports under `r2`. It ticks once a
second while an agent is running and holds its final value once the round
finishes. `Run flow` heads the strip when no round is selected, and a round
with no recorded agent time is headed `Round N flow` alone.

`Measured` shows the verified metric for the round that resolved the
hypothesis, as a delta against the last measurement preceding it once there is
one to compare against. The framework records a verified metric only when its
own official evaluation ran, on the sparse cadence or the final round, so a
hypothesis resolved between evaluations legitimately shows no measurement.

The table refetches when an agent phase or a round finishes, so it stays
current without being reopened. Rows are ordered by first round and never
reshuffle. Records written before hypothesis tracking render as
`(Unidentified)` rather than being dropped. Columns drop widest first as the
terminal narrows; hypothesis, rounds, and outcome always survive.

The chat is docked beside the table as a permanent column of this view, so a
question about the run never covers what the run has done. It has its own input
at the foot of its column, under the instruction `Ask about this run`,
and the client's command input sits beside it, starting at the boundary between
the two columns so each box is under the surface it writes to. The command
list rises out of the command input rather than across the chat.

The cursor starts in the command input, and `Ctrl+W` moves both it and the pane
keys to the chat and back; the chat's instruction line says so (`Ctrl+W to type
here`) until it holds them, and the focused input carries the focus border, so
where a keystroke lands is never a guess. A question typed into either box
reaches the same agent and is answered in the same column. Page Up and Page Down scroll the focused pane, and
Escape hands the keys back to the table. Arrows, Enter, and the rest of the
table's keys are unaffected by the dock. A slash command typed into the chat
input runs through the same path as anywhere else, and ordinary text typed into
the command input is still a question for the agent: the two boxes are a
division of attention, not of capability.

`/chat` leaves the command surface on this view, since the chat is already
beside the table: it is absent from `/help` and from the completions, though it
is still accepted and still focuses the pane. Inside a hypothesis, where the
chat is a dialog again, the command comes back and opens it over the
trajectory. It is one conversation throughout, so a question asked from the
pop-up is in the column when the operator steps back out.

The chat asks for the columns left over once the table has enough for its
claim, so a wide terminal loses no table column to it; where there is no such
surplus it still takes its readable minimum and the table drops columns as it
does on any narrow terminal. Below about 92 columns two columns would both be
unreadable, so the table keeps the row alone and the chat opens over it as a
modal. Opening a visualization narrows the chat before it narrows the table,
and where all three will not fit the chat is the one that steps aside.

Whichever way the chat is on screen, it never replaces the view it was asked
from: the modal floats over the table rather than swapping in the per-round
transcript behind it.

### Split panes

Visualization commands render beside the current view rather than over it.
`/perf` puts its output in a right pane and leaves the transcript, the chat,
or the experiment log in the left one, both live at the same time. On the
experiment log that makes three columns, chat, table, and visualization, each
live. A second visualization command replaces the pane's contents rather than
stacking another surface on top.

`Ctrl+W` moves focus one column to the right and wraps, through whichever
columns are actually on screen; the focused one carries the theme's focus
border and says so in its title. Page Up and Page Down scroll whichever pane
has focus, and Escape on the right pane closes it and restores the full-width
view. Chat and transcript state survive the pane closing.

Pane widths are computed from the terminal, so a wide terminal gives the
visualization real room while the left pane keeps a readable floor. Below 100
columns there is not enough width for both, and visualizations fall back to the
modal they used before panes existed. The layout re-flows on resize in either
direction.

`/help`, `/theme`, and errors stay modal.

### Experiment chat

The chat is answered by a coding agent scoped to the run, using the effective
backend, provider, and model configuration, with conversation state
carried across turns through `_vibesys_chat/conversation.jsonl` in the
workspace. The agent handler exists only while the run context does, so a
question asked during startup or after the run finishes has no agent to reach;
the reply says so and falls back to a read-only keyword summary of the recorded
events rather than presenting that summary as the answer.

Text typed in the chat that starts with `/` is parsed as a slash command
through the same path as the main input, so `/perf` there does exactly what
`/perf` does anywhere else. Anything else is a question for the agent, and a
question containing a slash mid-sentence is still a question.

Where the chat is docked it answers in its column, under its own input; where
it is not, inside a hypothesis or in a terminal too narrow to dock, it opens
over the view as before, carrying the same input at the foot of the modal. It
is one conversation either way: the transcript survives docking, undocking, and
the pane closing.

Inside a hypothesis the footer shows keyboard navigation. `[` and `]` select rounds, Tab and
Shift+Tab select agents, Page Up/Page Down scroll the transcript, Ctrl+T expands
todos, Ctrl+P expands the latest prompt in the current selection, Ctrl+L and
Escape return to the experiment log, and Ctrl+C exits. Commands listed under "Planned" in `/help`
are not accepted yet.

The launcher retains terminal results until the operator exits. If the backend
fails to start, its log tail is printed before the temporary session directory
is removed. Requests and subscription setup have bounded timeouts; malformed or
incompatible protocol messages are shown as errors instead of crashing a socket
callback.

## Themes

Four light/dark pairs ship: `dark` (default) / `light`, `solarized-dark` /
`solarized-light`, `catppuccin-mocha` / `catppuccin-latte`, and
`high-contrast-dark` / `high-contrast-light`. Selecting `dark` reproduces the
appearance the client had before themes existed: conversation cards, the
tool-call bands, and the Markdown palette are pinned to the original literals.
Four near-duplicate status shades were deliberately folded into the role they
belong to — a completed todo now uses the same green as an active agent phase,
completed phases and prompt-disclosure hints use the same blue as the detail
overlay, round labels use the same body-text color as card content, and the
chat panel's inner border matches its outer one. `theme.test.ts` pins all of
this so the baseline cannot drift.

Pick one with `--theme <name>`; launches without the flag use `dark`. The
launcher passes the selected name to the client through `VIBESYS_THEME`.
Inside a session, `/theme` opens the list as a selection: it starts on the
theme in use, Up and Down move the highlight, Enter applies it, and Escape
closes the list with the theme unchanged. Those keys belong to the picker while
it is open, so they never reach the view behind it, and a command typed into
the input still runs on its own Enter. `/theme <name>` re-themes every view in
place without opening the list.

`ui/theme.ts` is the only module holding color literals. A theme declares
semantic roles — `canvas`, `surface`, `elevatedSurface`, `selectedSurface`;
`textPrimary`, `textMuted`, `textSubtle`, `textStrong`; `border`,
`borderStrong`, `borderFocus`; `accent`, `info`; `success`, `warning`, `error`;
per-role conversation card colors; and Markdown/code colors. Views ask for a
role and never for a color.

Adding a theme means adding one `ThemeSpec`: a semantic core plus one accent
per conversation role. Card fills, labels, body text, the tool-call band, and
the Markdown palette are derived from that core, and each derived foreground is
pushed toward the nearest extreme until it clears the theme's `minContrast`
against the surface it actually sits on. The `dark` theme additionally pins its
derived values to the original literals so the baseline is byte-identical.
Status meaning never depends on color: agent phases carry a marker glyph and
the spelled-out status, todos carry a per-status marker, and only the running
round shows elapsed time.

## Architecture

The Python backend owns the validated, append-only event contract and serves it
as JSONL over a private Unix socket. `src/generated/` is generated from those
Pydantic models. The TypeScript client owns framing and request correlation,
`session-controller.ts` owns effects, `session-model.ts` and `run-map.ts` reduce
events into presentation state, and `ui/` owns OpenTUI rendering and input.

Conversation state retains at most 1,000 semantic entries. Rendering is keyed
by entry identity: state-only updates reuse existing cards, streamed tail
updates replace only the final card, and a full rebuild is reserved for filter
or history-window changes. Typed tool calls use stable call IDs so parallel
results return to the correct card; old event logs without IDs use a documented
FIFO-by-tool fallback.

## Development

From the repository root:

```bash
pnpm install --frozen-lockfile
pnpm --dir clients/tui generate:protocol
pnpm --dir clients/tui check
pnpm --dir clients/tui test
pnpm --dir clients/tui build
pnpm check:ts
uv run pytest tests/test_tui.py tests/agents/test_callbacks.py tests/render/test_sink.py
```

After changing Python protocol models, regenerate both files in
`src/generated/` and review their diff. The test suite covers reducer behavior,
OpenTUI frames and navigation, launcher cleanup, socket fragmentation and
timeouts, replay/live delivery, and the Python supervision service.
