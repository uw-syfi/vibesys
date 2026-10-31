# Independent visual review

The agent that built a screen has already decided it is fine. A fresh agent that
did not build it judges it instead, from pictures alone. This file is everything
the reviewer gets, apart from the screenshots.

## Brief the reviewer

Start a fresh agent, and give it only:

- this file and [visual-spec.md](visual-spec.md);
- real-size screenshots of the app window, one file per screen, never a contact
  sheet: a sheet shrinks screens until spacing and size cannot be judged;
- one line per screen saying what it is for, in the words of the user's task.

Do not give it the diff, the PR, the builder's reasons, or the calibration key.
Ask it to look at every picture at full size before it writes anything.
Unenforced: no check can see what a brief contained.

## Rubric

Ask each question of each screen. A "no" is a finding.

1. **Focus.** Where does the eye land first? Is that the thing that matters, and
   is there only one such place? (law 2)
2. **Depth.** Reading down, is each child smaller than or equal to its parent?
   Does anything at a lower level outweigh a title? (law 2)
3. **Edge.** Do the back control, the title and the content share a left edge?
   (law 1)
4. **Groups.** Can you see what belongs together from the spacing alone? Is any
   label as close to the rows above it as to its own? (law 3)
5. **Boxes.** Does each box fit what is in it? Is any box mostly empty, or any
   line of text stretched across a wide card? (law 4)
6. **States.** For each loading, empty or error state: is it no larger than what
   it stands in for? Does it have a card it does not need? Does it say what is
   happening and how far along it is, so a person can tell slow from stuck?
   (law 5)
7. **Labels.** For each group label: is there a parallel sibling? For each
   control: could a new user say what it does before they touch it, from its
   words alone? (law 6)
8. **Space.** For each strip, bar or margin: what does the user get for the room
   it takes? Would anyone miss it? (no law yet: a page cannot tell what a strip is
   worth)

## What to return

For each screen, a list of findings: the rubric number, where on the screen, what
is wrong in one sentence, and a severity of low, medium or high. Then one sentence
on what a user learns from the screen in three seconds. Report nothing as fine
that you did not check. Fix every finding rated medium or higher before the PR
opens, or say in the PR why not.
