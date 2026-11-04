# Independent visual review

The agent that built a screen has already decided it is fine. A fresh agent that
did not build it judges it instead, from pictures alone. This file is everything
the reviewer gets, apart from the screenshots.

## Brief the reviewer

Start a fresh agent, and give it only:

- this file and [visual-spec.md](visual-spec.md);
- real-size screenshots of the app window, one file per screen, never a contact
  sheet: a sheet shrinks screens until spacing and size cannot be judged;
- for each screenshot, its width in pixels and its scale (pixels per CSS pixel,
  2 on a retina capture);
- one line per screen saying what it is for, in the words of the user's task.

Do not give it the diff, the PR, the builder's reasons, or the calibration key.
Ask it to look at every picture at full size before it writes anything.
Unenforced: no check can see what a brief contained.

## How to measure

Screenshots differ in width and scale. Divide every pixel length by the
screenshot's scale to get CSS pixels, and compare sizes and gaps only within one
screenshot, or in CSS pixels. A still picture shows size, weight, colour and
position, and nothing else: judge those, and do not guess what moves.

## Severity

- **high**: a user would misread the screen or miss what it is for: the wrong
  thing draws the eye, a state cannot be told from another, a control cannot be
  understood.
- **medium**: a clear break of a question that a user would notice and flag, and
  who still understands the screen.
- **low**: a small break, seen only on a close look.

## Rubric

Ask each question of each screen, and do the test named in it. A "no" is a
finding. Write the test's numbers next to the finding.

1. **Focus.** List the three marks that stand out most, in order, by size, then
   by weight, colour and contrast. A "no" if the first is not what the screen's
   line says it is for, or if the second is within 25% of the first's size.
   (law 2)
2. **Depth.** Write down the title (the text that names the screen, wherever it
   sits, including a header row beside a back control) and its size. Then, for
   each level below it, in reading order (each heading, group label, menu or
   control label, row text, figure), write "larger", "same" or "smaller" than the
   title. A "no" for every one that is not smaller. A figure is text. (law 2)
3. **Edge.** Mark the x of the left end of each of these: the back control's icon
   (what is drawn, not its padding), the title's first letter, and the left edge
   of the first box or line of content. The sidebar and the window's own controls
   are not part of the page; leave them out. For a box, take its outer edge, not
   its inset text. A "no" when two differ by more than 2 CSS pixels. A screen
   with no back control compares the other two. (law 1)
4. **Groups.** For each label with rows under it, estimate three gaps: from the
   label to its first row (A), between the rows of the group (B), and from the
   group before it to the label (C). The gap values are not decided, so judge
   their order and never their pixels: yes only if A is smaller than C and B is
   smaller than C. A "no" when A is not smaller than C (the label hangs between
   two groups) or B is not smaller than C. (law 3)
5. **Boxes.** For each box (a card, a banner, a panel), compare its width with
   the span from its first mark to its last. A "no" when the marks span under 40%
   of it, or when a line of text is longer than about 105 characters. A row with
   a name at one end and an action at the other, far apart, is stretched, unless
   it is one of many equal rows of a table. (law 4)
6. **States.** For each loading, empty or error state, answer four things.
   Size: is it no larger than what it stands in for? Card: if its box were
   removed, would anything be lost? Words: does it say what is happening, in a
   noun? Progress: does it show a number, a step count ("2 of 5") or a time, so
   that a person can tell slow from stuck? A bar alone is not an answer to
   "how far along": a still picture of a bar with no number or time is a "no".
   (law 5)
7. **Labels.** For each group label: is there a parallel sibling? For each
   control, cover the rest of the screen and write one sentence saying what it
   does from its own words and icon. A "no" when you need "maybe" or two
   sentences, or when the sentence leaves out what it acts on. (law 6)
8. **Space.** List every horizontal band of the window from its top edge to its
   bottom edge (a full-width row: a top row, a header, a toolbar, the content, a
   footer), and every vertical column (a sidebar, a margin), with what each
   carries. A band that carries only the window's own controls (they may be
   drawn, or left blank in the picture) or nothing is a finding. For every other
   band or column, remove it in your head and move the rest up: if the user can
   still read and press everything they could, and can name nothing they lost,
   it is a finding. A band whose height is more than twice its tallest content
   is a finding too. (law 7)

## What to return

For each screen, a list of findings: the rubric number, where on the screen (in
the screenshot's own pixels, saying which screenshot), what is wrong in one
sentence, and a severity of low, medium or high. Then one sentence on what a user
learns from the screen in three seconds. Report nothing as fine that you did not
check. Fix every finding rated medium or higher before the PR opens, or say in
the PR why not.
