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
| The title strip is a waste of space | a, b, d | none | 8 |
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
