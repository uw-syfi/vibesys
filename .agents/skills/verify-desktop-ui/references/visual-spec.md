# Visual spec

The laws of how a desktop screen is laid out. Each one came from screens the
user rejected. A law a page can decide is a check in the screen suites; the rest
go to the independent reviewer, who judges against
[visual-review.md](visual-review.md).

Tokens stay as they are. Reference apps and density are not decided yet: do not
invent them, and do not move a value in the name of this spec.

## The laws

1. **One left edge per screen.** The back control, the title and the content
   start at the same x. Inset content is a defect, however little: a screen
   whose back chevron floats 9 px off its text reads as misaligned. Enforced by
   the `left-edge` check, to one pixel, from the ink of what is drawn (a button's
   empty padding does not count).
2. **Size follows depth.** A child is never larger than its parent, and a screen
   has one focal point: the title is the largest text in its page, and a heading
   is never larger than a heading above it. A figure is text here, so a tile's
   large ratio is either the screen's title or smaller than it. Enforced by the
   `heading-depth` check. Unenforced: whether the focal point is the thing that
   matters needs a reader.
3. **Spacing shows relationships.** The gap inside a group is smaller than the
   gap between groups, so that a reader can see the groups without a line or a
   box. Enforced by the `spacing-order` check over stacked rows. Only three
   named gaps exist:

   | Gap | Between | Value |
   | --- | --- | --- |
   | row | the rows of one group | PLACEHOLDER: not decided |
   | group | the groups of one section | PLACEHOLDER: not decided |
   | section | the sections of one page | PLACEHOLDER: not decided |

   Unenforced: which three values is a density decision the user has not made;
   name them here and in the tokens together, then add a check.
4. **Boxes fit their content.** A box as wide as its parent has to earn that
   width: only real data grids and tables fill it. Everything else fits, and
   prose keeps the line cap of about 105 characters. Enforced by the `hollow-box`
   check (a box at least 400 px wide whose content spans under 40% of it) and the
   `pane-fill` check (a wrapped column reaches 85% of its pane or the line cap).
5. **States stay small.** Loading, empty and error are no larger than what they
   stand in for, and have no card of their own. Their size is enforced by the
   `hollow-box` check, which also reads a progress bar with no value as a box with
   nothing in it. Unenforced: "no card of its own" needs to know what a box means,
   and a state that says what is happening and how far along it is needs a reader.
6. **Labels earn their place.** A group label appears only with a parallel
   sibling; a label or a control says what it does in the words of the user's
   task. Unenforced: whether two groups are parallel is a judgment of meaning, so
   a check would breed false positives.

## Reading a check's failure

A law's check fails on any screen the known-failure list does not explain. Fix
the screen; never loosen the threshold to pass one. When a law and an older
principle disagree, this spec wins and the principle changes in the same PR.
