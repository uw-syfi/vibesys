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
   in code or monospace-id elements. Enforced by the `raw-text` check.
4. **Plain words, few of them.** Prefer a chip, a count or a figure to a
   sentence. Say "failed a check", not the name of the condition. Unenforced:
   wording needs judgment.

## Show what matters, where it is expected

5. **A result goes at the end, with a one-line summary at the top.** A user
   learns the outcome in three seconds, then reads down for why. Unenforced:
   judgment.
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

## Sizes the checks cover

Windows of 1000, 1440 and 1920 px wide with the sidebar shown and hidden, live
and finished runs, long titles, every failure reason, retries, rounds, bases
that are not the input and runs of weeks. A screen that holds at 1440 and
breaks at 1000 is broken.
