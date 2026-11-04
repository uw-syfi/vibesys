# UX principles for the desktop app

What a user expects of a screen, built from what users have flagged. The critic
step in the `verify-desktop-ui` skill judges every screen against these. Each
principle names the screen check that enforces it, or says `Unenforced` and why.
When a user flags something new, add it here, and make it a check when a page
can decide it.

## Say what everything is

1. **Every color or series has a legend.** A chart dot, a step bar or a colored
   strip names what its color means, on the screen, next to it. Enforced by the
   `legend` screen check; whether the name is the right one is Unenforced
   (judgment).
2. **Numbers carry a label and a unit.** A figure says what it counts and in
   which unit (`48.6 ops/s`, `1.18×`, `8m`), or sits beside a label that does.
   Enforced by the `number-label` check, which sees a bare number and not a
   wrong or distant unit.
3. **No raw identifiers in text a person reads.** No snake_case, `${…}`,
   `undefined`, `NaN`, `null`, long hashes, raw enum values (a failure reason
   spelled as the program spells it) or Markdown markers. Identifiers belong
   in code or monospace-id elements (`data-mono-id`, which a code-styled `Text`
   and a mono field set). Nor code names: a harness, model or role id (`claude`,
   `opus-5-5`, `judge`) drawn as a label, any kebab-case identifier standing as a
   label, or a role's name in lowercase. Names are capitalised: "Judge",
   "Perf evaluator"; the backend owns them (`roleName`; harness and model names
   are agentshim's). Names the user typed (a project folder, an SSH alias) are
   theirs and listed in the check. Enforced by the `raw-text` check, whose ids
   come from the generated `CODE_IDS`.
4. **Plain words, few of them.** Prefer a chip, a count or a figure to a
   sentence. Say "failed a check", not the name of the condition. Unenforced:
   wording needs judgment.

## Show what matters, where it is expected

5. **A result goes at the end, with a one-line summary at the top when it is
   out of sight.** A user learns the outcome in three seconds, then reads down
   for why. A card already in view (a turn of thinking and a reply) needs no
   line above it that repeats it, and its header is one label: the kind, then
   the verdict. Unenforced: judgment.
6. **Nothing important is collapsed by default.** Fields a reviewer must read
   (a plan's fields, a reply's fields) are shown, not behind a toggle. Folding
   is for detail nobody has to read. Unenforced: judgment.
7. **Rows of one kind share one layout.** The same columns in the same order,
   the same state words, the same figures, at every level. Unenforced: judgment.

## Stay put and stay readable

8. **Charts stay mounted and keep their axes** when a user moves between
   levels or scrubs the run. Enforced by the stability check on the
   performance chart and the time bar (same element, same place, same size).
9. **Layout is stable between levels.** Moving down or up does not shift the
   header, the chart or the panes by a pixel. Enforced by the same check for
   the elements it anchors; Unenforced for the rest.
10. **Nothing wraps per character and nothing overlaps.** A chip, number or
    badge stays on one line; a word is never broken mid-word; text never
    draws over text. Enforced by the `wrap` and `overlap` checks.
11. **Nothing is cut without a way to read it.** Text cut with an ellipsis has
    a tooltip; no pane scrolls sideways; no text runs outside the window.
    Enforced by the `truncation` and `overflow` checks.
12. **A chart is big enough to read, and a state is never struck through.** The
    performance chart is at least 480 by 80 px (it drops under the figures
    when the row is narrow); text is never struck through, since that reads as
    deleted (a hidden filter is drawn without its pill, a failed count in the
    failed color). Axis and time-bar labels are thinned to the ones that fit,
    never overlapped. Enforced by the `chart-size` and `strike` checks and, for
    labels, the `overlap` check.
13. **Changes are visible, not jarring.** A live update is noticed (a row
    appears, a figure changes) without the page jumping, flashing or moving
    what the user is reading. Unenforced: needs a person watching.
14. **The sidebar stays a quick navigator.** New run and Settings stay in view while recent runs scroll; run rows and the sidebar toggle are clickable, the column resizes and collapses, and Settings has a way back to the exact prior screen. Enforced by `pnpm --filter @vibesys/app-desktop-shell test:e2e`.

15. **Layout follows the visual spec.** One left edge, size by depth, spacing by
    relationship, boxes that fit, small states, labels and bands that earn their
    place: the laws, and what enforces each, are in [visual-spec.md](visual-spec.md).

## Show failures, keep figures calm

16. **Failures are shown with the rest.** A failed idea is listed, in the tree and
    the list, with its reason in words; a filter may hide failures, never the
    default. Enforced by the ten-idea journey of the desktop shell's end-to-end suite.
17. **One figure per tile, in one voice.** A measure is a tile: its name in
    words, one large ratio with what it means ("faster than input"), a quiet line
    with its value and the reference, both with the unit, in one font. No chip
    competes with the figure. Enforced for the unit and the words by the same
    journey and the `number-label` check; whether the hierarchy is calm is
    Unenforced (judgment).
18. **A chart draws the measure, not the lineage.** The performance chart has
    its dots and one step line of the best so far; no line joins dots, and a
    chain the reader points at is ringed, not drawn. Enforced by the chart
    assertions of the ten-idea journey.
19. **Tokens are split, and unknown is never zero.** Input (with its cached
    share) and output are written apart; a part nobody reported reads
    `unknown`, a running turn `running`. Enforced by the token-words tests and
    the ten-idea journey.

## Trade-offs

18. **A trade-off chart says which way is better on each axis, and keeps its
    axes.** With two objectives the chart plots every idea on both, draws the
    frontier of the best trade-offs as one staircase, marks the input, mutes the
    dominated and turned-down ideas, and names an idea on hover with both values
    and units. Each axis title says its direction ("lower is better"), better is
    always toward the top and right, and the axes keep their range while the time
    bar moves, so scrubbing moves only points and the line. A tile per objective
    sits beside it, in the same place at every level. Enforced by the
    two-objectives journey of the desktop shell's end-to-end suite and the
    `legend` and `chart-size` checks; whether it reads calmly is Unenforced
    (judgment).

## Messages

20. **No banners: a passing failure is a toast, a problem with a thing is a status
    word at the thing.** No screen draws a full-width strip, tinted or not, for
    any state. "Could not save", "could not be read" and "did not take the
    command" are toasts at the top right of the window: a title and a tone, the
    detail behind a details button, kept until dismissed or cleared, never
    covering the title bar. A problem with one thing (a login, a machine, a
    section that could not be read, a step's transcript) is a chip at that thing
    with the detail as its hint and its action beside it. A hint is the chip's
    accessible description (read on hover, on keyboard focus and by a screen
    reader), so what a person must read to act, such as a login's link and code
    or a start that may have happened, is text on the page and never only a
    hint. A run's state is the header's status chip with a few words of why
    beside it, the whole of it as its hint, and Resume and Run again in the
    header; a diagnostic is a row of Logs with a dot on the Logs tab. A fact
    already on screen is not said again. Enforced for the component by its
    absence from `@vibesys/ui`, by the stylesheet test of the tone fills (only
    listed selectors may be washed with a tone, whatever they are called), by
    the `banner` screen check (no element at least half the main pane's width is
    filled with a status color, by color rather than by class name), by the
    `status-row` screen check (no alert, status or live region outside a toast, and no
    `Text` in a status tone, has words that reach half the pane, and no chip is that
    wide; the words are measured, not the box) and by the
    no-banners journey of the desktop shell's end-to-end suite; whether a
    message is passing or about a thing, whether a hint holds something a
    person must act on, a lone status word on a line of its own, and a bordered
    strip that is neither filled, announced nor toned, is Unenforced (judgment).

## Sizes the checks cover

Windows of 1000, 1440 and 1920 px wide with the sidebar shown and hidden, live
and finished runs, long titles, every failure reason, retries, rounds, bases
that are not the input and runs of weeks. A screen that holds at 1440 and
breaks at 1000 is broken.
