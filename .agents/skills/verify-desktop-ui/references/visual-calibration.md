# Calibrating the checks and the reviewer

Screens the user rejected, with what they said. A check or a rubric that cannot
find these will not find the next one. Recalibrate after changing a law, a
geometry check or the rubric. This file is the key: never give it to the reviewer.

## The set

Each file is in the skill's calibration assets. The one line is what the reviewer
is told about the screen; it says what the screen is for, never what is wrong.

| Screen | Told to the reviewer |
| --- | --- |
| [a](../assets/calibration/screen-a.png) | The runs page while the app reads the list of runs. |
| [b](../assets/calibration/screen-b.png) | The runs page while a machine connects. |
| [c](../assets/calibration/screen-c.png) | A finished run. |
| [d](../assets/calibration/screen-d.png) | The runs page, once runs are listed. |
| [e](../assets/calibration/screen-e.png) | Settings, on the agent logins page. |

Screens a and d are fixture renders, and b, c and e are captures of the real app;
c had its project name and run id replaced. Screens that change later stay here:
they are the record of the complaint, not of today's app.

## The key

Every row must be found. "Check" names the check that finds it on the same screen
rendered from a fixture: a check reads a live page, so it cannot read these files.
"Reviewer" is the rubric question that must lead a blind reviewer to it.

| Complaint | Screen | Check | Reviewer |
| --- | --- | --- | --- |
| A bare loading bar, with no sense of progress | a | `hollow-box` | 6 |
| A wide card for three short lines, after the bar | b | `hollow-box` | 5, 6 |
| The run screen's hierarchy is all messed up | c | `heading-depth` | 1, 2 |
| What the grouping menu does is not intuitive | d | none | 7 |
| The title strip is a waste of space | a, b, d | `title-strip` | 8 |
| "This app" is a stray label | e | none | 4, 7 |
| The back control and title miss the content's edge | e | `left-edge` | 3 |
| The title is smaller than the menus under it | e | `heading-depth` | 2 |

## Run it

1. Render each screen from a fixture and run the screen checks with every check
   on: the rows with a check must show that check on that screen. Where the app
   has moved on, the screen's known-bad page stands in for it.
2. Brief a fresh agent as [visual-review.md](visual-review.md) says, with the
   screens and the lines above, and nothing else from this file.
3. Compare its findings with the key. A finding counts if it names the same
   defect at the same place in any words.
4. The bar: the checks and the reviewer together find every row. A row only the
   reviewer finds has no check holding it, so a change that misses it reaches the
   user unless a reviewer runs.

Unenforced: a blind reviewer is an agent's judgment, so CI cannot run it. The
checks' half is held by the known-bad pages of the screen check suite.

## Rounds

Each blind run, scored against the key, and what changed after it. Add a round
whenever a reviewer is run; the next round starts from the rubric as it stands.

### Round 1

The reviewer found the bare loading bar (a), the wide card (b), the run
hierarchy (c), the grouping menu (d, as a control that does not say whether it
sorts or filters), the stray group label (e), and the back control and title off
the content's edge (e).

It missed the title strip as wasted space (a, b, d), which no check held: the
bar failed the calibration bar. Question 8 asked "would anyone miss it?" with no
test, so the reviewer only said what it would miss itself. It also missed that
the title was smaller than the menus under it (e); `heading-depth` holds that
row, and question 2 did not say to compare the title with each level below it.

It listed as unclear: severity, which was undefined; question 1, subjective on a
still picture; question 3, whether the sidebar or a card's inset text counts
toward the left edge; question 4, which could not be applied while the gap values
are not decided; question 6, whether a bar alone answers "how far along";
question 8, with no test; and screenshots of different width and scale.

What changed:

- Question 8 now lists every horizontal band and vertical column with what it
  carries, flags a band carrying only the window's own controls, and gives a
  removal test for the rest. The new `title-strip` check holds the top strip, so
  the row has a check as well as a question; it fires on the screens of every
  suite today, each listed as a known failure.
- Question 2 now compares the title with each level below it, one by one.
- Severity is defined. The brief gives each screenshot's width and scale, and
  lengths are read in CSS pixels. Question 1 is a ranked list of marks. Question 3
  names what is left out (the sidebar, window controls, a card's inset text) and
  allows 2 CSS pixels. Question 4 judges the order of three gaps. Question 6 says
  a bar with no number or time does not answer "how far along".

Not yet shown: that the changed rubric leads a fresh reviewer to the title strip
and to the small title. Run the next blind reviewer and score it here.
