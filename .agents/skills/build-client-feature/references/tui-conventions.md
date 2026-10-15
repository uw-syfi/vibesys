# TUI display and key conventions

Open this when a change to the terminal UI adds or moves a key binding, changes
which pane holds focus, or changes how a pane is sized or drawn. The terminal
UI (TUI) is deprecated but maintained, so these rules hold until it is deleted.

The long-form rationale, with its prior-art sources (IBM Common User Access,
WCAG 2.2, `less`/`vi`/`tig` habits, readline), is
[docs/contributing/tui/conventions.md](../../../../docs/contributing/tui/conventions.md),
and the comparison of candidate keymaps is
[keybinding-matrix.md](../../../../docs/contributing/tui/keybinding-matrix.md).
This file restates only the rules the code keeps today, checked against
the key router. Terms: a **pane** is a bordered region
that can take keys (a `PaneId`); **focus** is which pane receives keys;
**zoom** gives the whole content row to one pane.

## Where the rules live in code

- The TUI has one key router. Handlers run top to bottom, and the first that
  claims a key calls `preventDefault` and returns, so a modal's handler above
  shadows everything below it. Put a new binding at the level of the surface
  that owns it.
- The session model has a single authority for which pane wears the focus
  treatment. Keys that act on "the focused pane" ask it, rather than reading
  the round or layout focus directly, so the key target and the lit border
  cannot disagree.
- One module draws the focus treatment (pane title and border style), and a
  test holds it for every built-in theme.
- Each resizable pane owns its floor, ceiling and clamp, and the resize step
  is shared. Do not restate their numbers in prose or tests; import them.

## Rules in force

**Typing wins.** Single-key bindings (`d`, `<`, `>`, `=`, `[`, `]`, the arrow
keys for pane movement) act only while the command input is empty
(`inputIsEmpty`). A typed command containing that character is never hijacked.

**Focus is never carried by colour alone.** A focused pane shows a marker
glyph in a gutter cell its title always reserves, so the title does not shift
on focus; the border colour reinforces it. Border weight is not a channel: the
frame shape is the same focused or not, because Unicode has no heavy rounded
corner. A focus test holds it.

**One surface is lit, and it is a pane.** A box nested inside a pane (the chat
composer, the command box) keeps the resting frame. A surface that takes the
arrow keys (the expanded todo list, a visualization drawn as an overlay on a
narrow terminal) is a pane. The rounds rail is the exception: it draws its own
focus border from `roundFocus` while `focusedPane` reports the agents side, so
`F4` zooms the agents pane.

**Arrow keys in the round view.** `←` and `→` step across rounds rail, agents,
transcript, and clamp at the ends; when the rail is hidden
(its column width is 0) the order is agents,
transcript. `↑` and `↓` step rounds while the rail holds focus, agents while
the agents pane does, and transcript entries otherwise. A focus left on a
hidden rail falls through to the agents pane, so keys never drive an invisible
selection. `Tab` and `Shift+Tab` step agents once input completion declines
the key; `[` and `]` step rounds.

**Pane focus and global keys.** `Ctrl+W` cycles pane focus when a right pane
or the docked chat is present. `Ctrl+L` returns to the live log from any live
pane, including a focused docked chat. `F1` opens the command palette, `F2`
(or `Ctrl+T`) toggles todos, `F3` (or `Ctrl+P`) the latest prompt, `F4` zooms
the focused pane, `F5` opens the private notepad; inside the notepad, `F6` and
`F7` promote its text to a steer or chat draft. The only command prefix is
`/`; there is no search.

**Esc goes back one level.** It cancels a modal, closes an overlay, leaves a
drill-down, or returns focus to the left column. It never quits. A modal keeps
its Escape for itself even when an error banner is showing.

**`d` opens the diff of the selected round** from the design pane and from a
round drill-down. The diff viewer is a modal: `←`/`→` move by file, `↑`/`↓` by
hunk, and Escape closes the viewer alone.

**A resize moves whole columns, and only columns.** `<` and `>` change the
width of the round view's agents pane or the docked chat by the resize step;
`=` clears the explicit width and restores automatic sizing. `=` is the way
out, so it is not gated on the pane being resizable. A press that would not
change the rendered width stores nothing, so an explicit width survives a
terminal that narrows and comes back. A resize never swaps a pane's
presentation: `<` stops at `agentGraphMinWidth` rather than dropping the agent
graph to the stacked list, and the rounds rail has a fixed width no key moves.

**Nothing moves that does not have to.** Reserve space for anything that
appears conditionally (markers, status lines, counts, variable-width numbers),
so a row appearing does not shift the list under the cursor.

**Bindings are visible.** The key-help line at the bottom spans the full
screen width and changes with the surface in front.

**A fill lives on an inner box.** A bordered box with a background colour
paints its border ring too, which bleeds past rounded corners. Put the
fill on an inner layer through the shared fill-layer helper. An
overlay that floats over content keeps an outer fill and therefore draws a
square border. A test over the whole constructed tree holds it. OpenTUI turns a border back on when
`borderStyle` or `borderColor` is passed beside `border: false`, so a
borderless box omits both.

## Adding or moving a binding

1. Name the rule above that the binding follows. If none covers it, add one to
   the Rules in force section of
   [conventions.md](../../../../docs/contributing/tui/conventions.md) in the
   same change, citing the keybinding-matrix entry it follows.
2. Add a case to the keybinding tests that drives the key
   through the router and checks the resulting state.
3. Update the key-help line for the surface that owns the binding.
4. A layout consequence (widths, clipping, overlap) gets a `createTestRenderer`
   test of the real view at a fixed size; see
   [tui-architecture.md](../../../../docs/contributing/tui-architecture.md),
   "Regression tests for rendering bugs". Keys through a real terminal are
   exercised by the `hunt-client-bugs` skill, not by CI.
