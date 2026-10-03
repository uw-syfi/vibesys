# TUI keybinding source matrix

This is a research document, not a keymap. It compares how six sources bind
six functions the TUI will eventually need a convention for: pane/window
switching, movement within a pane, search, command entry, help, and quitting
a read-only view. It exists because
[#711](https://github.com/uw-syfi/vibesys/issues/711) found the TUI making
the opposite argument to itself: the same key family already means two
things depending on context (arrows sometimes move within a pane, sometimes
jump between panes), and several keys the
["Proposed: movement and naming"](./conventions.md#proposed-movement-and-naming)
section proposed are either unbound or already claimed for something else.
Choosing a keymap
before this comparison exists is the failure mode `adi/tui-keymap-defaults`
(`dac09f1`, no PR) is cited for in #711; this document does not choose one.
No binding here is added, moved, or implemented as part of publishing this
page.

## How to read the tables

Each row names a source, the binding that source documents, the exact
context that binding applies in (a mode, a view, a state), and whether the
binding still fires when the TUI's own composer (the chat/command input) is
focused and holds typed text. That last column matters because this TUI's
text-entry contexts already promise to keep Readline's cursor, deletion,
history, completion, interrupt, and EOF behavior; a candidate binding that
collides with a Readline chord cannot fire while the composer is non-empty,
whatever the source proposes.

`N/A` means the source has no concept that maps onto the function (Readline
has no window to switch to). `Not documented` means the source might bind
something here, but the citation below could not confirm it; see
[Unverifiable citations](#unverifiable-citations) rather than treating the
gap as "no binding exists."

## Sources

1. **Claude Code** — [Interactive mode](https://code.claude.com/docs/en/interactive-mode),
   the official keyboard-shortcut reference, accessed 2026-09-21.
2. **Codex CLI** — [Developer commands](https://developers.openai.com/codex/guides/slash-commands),
   which redirects (308) to `https://learn.chatgpt.com/docs/developer-commands?surface=cli`,
   the `surface=cli`-rendered version of OpenAI's shared commands page,
   accessed 2026-09-21.
3. **Vim** — [vimhelp.org](https://vimhelp.org/index.txt.html), the online
   mirror of `:help` (current for Vim 9.2): `index.txt` (the `:help index`
   tag list), `windows.txt` (`CTRL-W` commands), `motion.txt` (cursor
   motions), and `repeat.txt` (macro recording).
4. **LazyVim** — the [Keymaps reference](https://www.lazyvim.org/keymaps) and
   [`lua/lazyvim/config/keymaps.lua`](https://github.com/LazyVim/LazyVim/blob/main/lua/lazyvim/config/keymaps.lua)
   on the `main` branch, accessed 2026-09-21. LazyVim keymaps sit on top of
   Vim's defaults; a function with no LazyVim-specific row inherits Vim's row
   unless noted otherwise.
5. **GNU Readline**, default (emacs mode) keymap — cross-checked against
   [readline.kablamo.org/emacs.html](https://readline.kablamo.org/emacs.html).
   The canonical citation is the GNU Readline manual's "Commands For Moving,"
   "Commands For History," and "Killing And Yanking" sections at
   `https://www.gnu.org/software/bash/manual/html_node/Readline-Interaction.html`;
   that page returned HTTP 429 (rate-limited) during this research and could
   not be fetched directly. The bindings below are the long-stable Readline
   emacs-mode defaults (unchanged across current Readline versions) and match
   the secondary source cited; flagged here rather than silently cited as
   gnu.org.
6. **WCAG 2.2** — the W3C Understanding documents for each success criterion,
   linked per row. `tui-conventions.md`'s [Sources](./conventions.md#sources)
   section already cites 2.1.1, 1.4.1, 2.4.3, and 2.4.7 for other rules in
   force; this matrix adds 2.1.2 No Keyboard Trap and 2.4.11 Focus Not
   Obscured (Minimum), both squarely about the "switch panes" and "quit a
   view" functions below.

## Pane / window switching

| Source | Binding | Context | Holds with a non-empty composer? | Notes |
| --- | --- | --- | --- | --- |
| Claude Code | No peer-pane jump; `Ctrl+O` toggles the transcript viewer, `Left`/`Right` cycle tabs in a dialog | General controls (any state) | `Ctrl+O`: yes, it is a general control, not composer-gated | Claude Code has one input surface, not a pane grid. `Ctrl+O` swaps to a secondary *view* rather than moving focus among peers, and dialog-tab cycling only exists while a dialog is open |
| Codex CLI | Not documented | — | — | The fetched commands page documents session-level slash commands (`/agent`, `/ide`) but no raw keystroke for moving focus between TUI regions. See [Unverifiable citations](#unverifiable-citations) |
| Vim | `Ctrl-W` then `h`/`j`/`k`/`l` moves to the window in that direction; `Ctrl-W w` / `Ctrl-W W` cycle next/previous; `Ctrl-W p` returns to the last-accessed window | Normal mode, multiple windows | No — Insert mode inserts these as literal text; `Esc` first returns to Normal mode | `Ctrl-W` is a two-key prefix: the second key selects the window command, it is not a single chord |
| LazyVim | `Ctrl-h`/`Ctrl-j`/`Ctrl-k`/`Ctrl-l`, `remap=true` aliases of Vim's `Ctrl-W h`/`j`/`k`/`l` | Normal mode | No — same Insert-mode exclusion as Vim | LazyVim keeps Vim's `Ctrl-W` prefix (`Ctrl-W w`, `Ctrl-W p`, etc. are unchanged) and adds single-chord aliases only for the four directional moves |
| GNU Readline | N/A | — | — | A line editor has no window concept |
| WCAG 2.2 | No specific key; requires [2.1.1 Keyboard](https://www.w3.org/WAI/WCAG22/Understanding/keyboard) (switching must be keyboard-reachable), [2.4.3 Focus Order](https://www.w3.org/WAI/WCAG22/Understanding/focus-order) (the order panes receive focus in must be predictable), [2.1.2 No Keyboard Trap](https://www.w3.org/WAI/WCAG22/Understanding/no-keyboard-trap) (switching away from a pane must not get stuck), and [2.4.11 Focus Not Obscured (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum) (the newly focused pane must not be entirely hidden by an overlay) | N/A | N/A | A requirement, not a binding |

## Movement within a pane

| Source | Binding | Context | Holds with a non-empty composer? | Notes |
| --- | --- | --- | --- | --- |
| Claude Code | `Up`/`Down` or `Ctrl+P`/`Ctrl+N` move the cursor within multiline input first, then step command history once the cursor is on the first/last visual row | Prompt input (the composer itself) | Yes — this binding is defined for the composer, so "non-empty" is its normal case; the fork is on cursor row, not on emptiness | The row/history split means the same key genuinely means two things depending on cursor position, the same asymmetry #711 flags for this TUI's own arrows |
| Codex CLI | `Up`/`Down` restore draft history | Composer | Documented as a plain composer binding, but the source does not state whether it forks on cursor position the way Claude Code's does | Cannot confirm the exact rule beyond "Up/Down restores draft history"; treat the row/history split as Claude-Code-specific until Codex's own docs say otherwise |
| Vim | `h`/`j`/`k`/`l` move left/down/up/right by character/line | Normal mode | No — Insert mode exclusion, `Esc` first | `h`/`l` are exclusive motions, `j`/`k` are linewise; matters when combined with an operator (`dj`, `yk`), not relevant to plain movement |
| LazyVim | Inherits `h`/`j`/`k`/`l`; remaps bare `j`/`Down` to `gj` and `k`/`Up` to `gk` when no count is given | Normal, Visual modes | No — same Insert-mode exclusion | `gj`/`gk` move by displayed (wrapped) line instead of logical line; the remap only fires without a preceding count so `5j` still moves 5 logical lines |
| GNU Readline | `Ctrl-f`/`Ctrl-b` (forward-char/backward-char), `Alt-f`/`Alt-b` (forward-word/backward-word), `Ctrl-p`/`Ctrl-n` (previous-history/next-history) | Any Readline-driven line, always | Yes, intrinsically — this is the composer's own movement layer | Arrow keys are conventionally bound to the same functions via the terminal's terminfo capability strings in a stock `inputrc`, but that mapping is a default *configuration*, not a Readline binding this citation set verified directly |
| WCAG 2.2 | No specific key; requires [2.1.1 Keyboard](https://www.w3.org/WAI/WCAG22/Understanding/keyboard) | N/A | — | Focus order (2.4.3) is about which component receives focus next, not about movement inside one already-focused component, so it is not cited again here |

## Search

| Source | Binding | Context | Holds with a non-empty composer? | Notes |
| --- | --- | --- | --- | --- |
| Claude Code | `Ctrl+R` reverse-searches prompt (command) history | Prompt input | Yes — `Ctrl+R` is itself a composer binding | This is history search, not content search of a pane. Claude Code deliberately does not bind pane-content search: pressing `[` in the transcript viewer writes the conversation to the terminal's native scrollback specifically so `Cmd+F`/tmux copy mode can search it, punting the function to the terminal rather than competing for a key |
| Codex CLI | `Ctrl+R` searches prompt history; press `Enter` to use a match or `Esc` to cancel | Composer | Yes — same reasoning as Claude Code | Also history search, not pane-content search. Codex and Claude Code agree on `Ctrl+R` for this because both inherit it from Readline (below), not because they agree with each other independently |
| Vim | `/{pattern}` searches forward, `?{pattern}` searches backward | Normal mode | No — Insert mode exclusion, `Esc` first | Genuine content search over the buffer, not history search |
| LazyVim | Inherits `/` and `?`; `n`/`N` step to the next/previous match and unfold it (`zv`); `<leader>sg` / `<leader>/` grep the project root, a different, wider scope than in-buffer search | Normal mode | No — same Insert-mode exclusion for `/`/`?`; the `<leader>` forms are also Normal-mode only | Two different "search" scopes coexist: in-buffer (`/`) and project grep (`<leader>sg`). A TUI convention that says "search" without naming the scope will collide with this precedent, not follow it |
| GNU Readline | `Ctrl-r` reverse-search-history, `Ctrl-s` forward-search-history | Any Readline-driven line, always | Yes, intrinsically | Same function Claude Code's and Codex's `Ctrl+R` both delegate to. `Ctrl-s` is frequently intercepted by terminal flow control (XON/XOFF) unless `stty -ixon` is set, a caveat from general terminal behavior, not from the Readline manual itself |
| WCAG 2.2 | No specific key; requires [2.1.1 Keyboard](https://www.w3.org/WAI/WCAG22/Understanding/keyboard); if search opens an overlay, also [2.1.2 No Keyboard Trap](https://www.w3.org/WAI/WCAG22/Understanding/no-keyboard-trap) and [2.4.11 Focus Not Obscured (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum) | N/A | N/A | A requirement, not a binding |

## Command entry

| Source | Binding | Context | Holds with a non-empty composer? | Notes |
| --- | --- | --- | --- | --- |
| Claude Code | `/` at the start of input opens the command menu; `/` after a space plus letters also completes a command mid-prompt | Prompt input | No for the leading-`/` case (it only opens the menu at input start); the mid-prompt case is a distinct, narrower completion feature, not a general "holds while non-empty" guarantee | `:` is bound to something else entirely here: typing `:name:` inserts an emoji shortcode. That is a second, unrelated overload of the character the old proposal in `conventions.md` wanted for commands |
| Codex CLI | `/` opens the composer's slash-command popup | Composer | Documented for the composer generally; the source does not specify a start-of-line restriction the way Claude Code's does | — |
| Vim | `:` starts an Ex command line | Normal mode | No — Insert mode exclusion, `Esc` first | — |
| LazyVim | Inherits `:`; `<leader>:` opens command *history* (browsing past Ex commands), a different function from entering a new one | Normal mode | No — same Insert-mode exclusion | — |
| GNU Readline | N/A | — | — | A shell line already is the command; Readline has no secondary command-prefix within a line |
| WCAG 2.2 | No specific key; requires [2.1.1 Keyboard](https://www.w3.org/WAI/WCAG22/Understanding/keyboard) | N/A | — | — |

## Help

| Source | Binding | Context | Holds with a non-empty composer? | Notes |
| --- | --- | --- | --- | --- |
| Claude Code | `?` on empty input toggles the shortcut help panel | Prompt input | No, explicitly: the documentation states typing `?` when the input already has text inserts the literal character instead | No `F1` help binding appears anywhere in the fetched reference; do not assume CUA's F1-for-help is honored here |
| Codex CLI | Not documented | — | — | No `?` or `F1` help binding found in the fetched commands page or in the project's own GitHub issues about `/keymap` and hotkeys. See [Unverifiable citations](#unverifiable-citations) |
| Vim | `<F1>` is the same as `<Help>`, which opens a help window; `:help {subject}` is the Ex-command form | Normal mode (and most others) | No — Insert mode exclusion, `Esc` first (Ex-command form) | Vim's `F1`-for-help is one of the places Vim and CUA already agree, independent of each other |
| LazyVim | `<leader>?` (buffer keymaps via which-key), `<leader>sh` (help pages), `<leader>sk` (keymaps) | Normal mode | No — same Insert-mode exclusion | All three are multi-key leader sequences, not a bare `?` or `F1`. Whether LazyVim leaves Vim's own `<F1>`/`<Help>` bound was not confirmed in the fetched keymaps reference; treat as "inherited unless a LazyVim extra says otherwise," not as verified |
| GNU Readline | N/A | — | — | Readline has no in-line help view; `man bash`, `info readline`, and `bind -p` are external to the editing session, not Readline key bindings |
| WCAG 2.2 | No specific key; requires [2.1.1 Keyboard](https://www.w3.org/WAI/WCAG22/Understanding/keyboard) | N/A | — | WCAG has no success criterion specific to a help feature; the only applicable requirement is that whatever help mechanism exists must be keyboard-operable like everything else |

## Quitting a read-only view

| Source | Binding | Context | Holds with a non-empty composer? | Notes |
| --- | --- | --- | --- | --- |
| Claude Code | `q`, `Ctrl+C`, or `Esc` exit the transcript viewer (all three rebindable via `transcript:exit`) | Transcript viewer (a read-only overlay, opened with `Ctrl+O`) | Not applicable in this context: the transcript viewer is a separate view, not the composer | The clearest precedent among the six sources for "`q` quits a read-only view," and it is a real, current, shipped binding, not a proposal |
| Codex CLI | `Ctrl+C` or `/exit` close the whole session | Session-level | — | No distinct read-only sub-view with its own quit key was found in the fetched documentation; `Ctrl+C`/`/exit` end the CLI entirely, a different scope than "close this one view." See [Unverifiable citations](#unverifiable-citations) |
| Vim | `:q`, `ZZ` (write if changed, then close window), `ZQ` (close without writing), `Ctrl-W q` (quit current window, like `:quit`) | Normal mode | No — Insert mode exclusion | Bare `q` in vanilla Vim Normal mode does **not** mean quit: it starts recording a macro into the register named by the next keystroke (`q{register}`), and a second bare `q` stops an in-progress recording. The `less`/`man`/`htop`-style "`q` quits a read-only view" convention is a pager convention, not a core Vim Normal-mode default; Vim's help window and quickfix window carry their own buffer-local `q`-to-close mapping layered on top of, not instead of, macro recording elsewhere |
| LazyVim | `<leader>bd` (delete buffer), `<leader>bD` (delete buffer and window), `<leader>wd` (delete window), `<leader>qq` (quit all) | Normal mode | No — same Insert-mode exclusion | All leader-prefixed; LazyVim does not rebind bare `q` away from macro recording. The buffer-local `q`-to-close mapping on Vim's help/quickfix windows is inherited from Vim, not added by LazyVim |
| GNU Readline | N/A | — | — | A line editor has no read-only view to quit |
| WCAG 2.2 | No specific key; requires [2.1.2 No Keyboard Trap](https://www.w3.org/WAI/WCAG22/Understanding/no-keyboard-trap) (the view must be leavable via keyboard, and if the method is non-standard, the user must be told how) and [2.1.1 Keyboard](https://www.w3.org/WAI/WCAG22/Understanding/keyboard) | N/A | — | This is the criterion the "bindings are visible" rule in [Rules in force](./conventions.md#bindings-are-visible) already satisfies in spirit: the key-help line at the bottom of the screen is exactly the "advise the user of the method" 2.1.2 asks for when the exit key is not a standard one |

## Conflicts

These are recorded as conflicts, not resolved into a preference. A future
keymap PR has to pick a side and say so; this document does not pick for it.

- **Arrow-key semantics.** CUA's rule (cited in `conventions.md`'s
  [Sources](./conventions.md#sources)) is that arrows move within the
  focused pane and Tab moves between panes. This TUI's current bindings do
  not follow that split: inside the left column, `←`/`→` jump between the
  agent strip and the transcript (pane-to-pane-like), while `↑`/`↓` move
  within whichever holds focus. Vim and LazyVim resolve the same ambiguity
  differently again: `h`/`j`/`k`/`l` (or arrows) always move within a
  buffer, and a *separate* prefixed chord (`Ctrl-W` + direction) is
  window-to-window, so the same physical key never carries both meanings.
  Claude Code's own composer has a third split: `Up`/`Down` move the cursor
  until it hits the first/last visual row, then the same key starts walking
  history, so one key already carries two meanings there by cursor position
  rather than by pane. Three real precedents, three different rules; none of
  them is "arrows sometimes mean pane-to-pane and sometimes mean
  within-pane, depending on which pane you're already in," which is what
  this TUI does today.
- **`Ctrl+W` overload.** GNU Readline binds `Ctrl-w` to `unix-word-rubout`,
  deleting back to the previous whitespace, in every default Readline text
  field, including this TUI's own composer per its Readline-compatibility
  promise. Vim and LazyVim bind `Ctrl-W` as the window-command prefix in
  Normal mode. This TUI binds `Ctrl+W` to pane focus today. All three are
  live, current, shipped conventions for the same chord; none of them is
  wrong on its own ground, and adopting any one of them for "pane focus"
  guarantees a collision with at least one of the others the moment the
  composer holds text (Readline's claim on `Ctrl-w` fires there
  unconditionally, per the table above).
- **`/` overload.** `/` is the closest thing to unwritten cross-tool
  consensus for "search" (`less`, `vi`/Vim, `man`, `htop`, `tig`, `k9s`, per
  `conventions.md`'s de facto terminal conventions). It is also this TUI's
  only command prefix today (`/help`, `/theme`, `/open-round`), and it is
  Claude Code's and Codex's command-menu key. A future PR that wants both
  "search" and "command" cannot bind both to `/`; `conventions.md` already
  flags renaming the command prefix to `:` as a large, separate,
  deprecation-requiring change, and this matrix adds that `:` is not free
  either: Claude Code repurposes bare `:name:` for emoji shortcodes, a third
  claimant on the same character across the sources compared here.

## Unverifiable citations

Flagged explicitly rather than guessed:

- **Codex CLI pane/window switching.** No raw keystroke for moving focus
  between TUI regions was found in the fetched `developer-commands` page.
  Codex's `/keymap` command and its `[tui.keymap]` `config.toml` section
  imply *some* set of rebindable TUI actions exists, but the default keymap
  itself was not enumerated anywhere this research reached.
- **Codex CLI help binding.** No `?` or `F1` binding, and no `/help`
  command, appears in the fetched documentation or in a search of Codex's
  own GitHub issues about keybindings. This may mean Codex has no dedicated
  help key, or it may mean the fetched page is incomplete; either way, it is
  not confirmed rather than confirmed-absent.
- **Codex CLI read-only view quit key.** `Ctrl+C`/`/exit` end the whole
  session in the documentation reached; no distinct read-only sub-view with
  its own narrower quit key was found.
- **GNU Readline manual, primary source.** `gnu.org`'s own Readline
  Interaction page returned HTTP 429 during this research and could not be
  fetched directly. The bindings cited above are cross-checked against
  `readline.kablamo.org`'s emacs-mode reference instead; they are long-stable
  Readline defaults, but this document did not read them from the canonical
  manual page itself.
- **LazyVim and bare `<F1>`/`<Help>`.** The fetched LazyVim keymaps
  reference does not mention `F1` at all. Whether Vim's own `<F1>` binding
  survives untouched in a default LazyVim install, or is shadowed by some
  extra not covered in the core keymaps reference, was not confirmed.
- **Readline arrow-key bindings.** Readline's manual documents named
  functions (`forward-char`, `previous-history`, ...) bound to named keys
  (`C-f`, `C-p`, ...). That arrow keys conventionally invoke the same
  functions via terminal terminfo capability strings in a stock `inputrc` is
  general Readline/terminal knowledge, not a binding this research verified
  against the manual directly.

## Superseded

This document supersedes the "Naming" table and its framing in
[`conventions.md`'s "Proposed: movement and naming"
section](./conventions.md#proposed-movement-and-naming). That table compared
a narrower set of sources (`less`, `vi`, `man`, `htop`, `tig`, `k9s`, and
CUA) to a fixed list of candidate keys. This matrix compares the six sources
#711 asked for, records where they conflict instead of implying a single
proposed default, and adds the non-empty-composer question the old table did
not ask. The "Movement" subsection's description of today's arrow-key
asymmetry is still accurate as a statement of current behavior; its citation
of CUA as *the* replacement rule is superseded by the fuller comparison in
[Conflicts](#conflicts) above, which shows CUA is one of at least three
live precedents for splitting within-pane and between-pane movement, not the
only one.

As before: no binding is chosen here. A PR that adopts a specific default
still names the rule it follows, per
[Applying this](./conventions.md#applying-this), and can now cite this
matrix instead of re-deriving the comparison.
