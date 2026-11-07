---
name: verify-desktop-ui
description: Verify a desktop UI change the way a user sees it, before its pull request opens: look at the screens on the fixture page, produce the app-window contact sheet, judge each screen against the UX principles and the visual spec, and have an independent reviewer judge it. Use when changing the desktop app's screens (the runs list, Start run form, a run's agent map, an agent), fixing a visual or wording bug there, adding a state a user can see, or opening a pull request that touches them.
metadata:
  kind: workflow
---

# Verify desktop UI

Tests of a UI change pass and the screen is still wrong: a ratio wrapping one
character per line, overlapping header text, a raw failure reason, a chart that
vanishes while scrubbing. This skill catches those before the user does,
cheaply and the same way every time. The checks read real layout in a real
browser against realistic runs; the critic step and the independent reviewer
cover what no check can.

## Verification order

Use the cheapest tier that can show the defect, and stop there.

1. **The fixture page** (seconds): the app's screens over a scripted fake daemon, in a
   browser. Every UI change starts and usually ends here.
2. **Agent-less runs** (minutes): the real backend with scripted agents and no
   model calls, on the CPU queue example. From a project copied out of the
   `queue-rs` example, run `vibesys --task mpmc --outer-loop dynamic
   --agent-backend scripted --max-rounds 5 --max-in-flight 2`. The scripted agents' seed is the
   `scripted_seed` setting of the agent configuration, so a run repeats. Use them
   for what the fake daemon cannot show: startup, a real reconnect, an ended run.
3. **Real runs with live agents**: only at the end of a batch of UI changes, or
   when the user explicitly grants one. Use Codex with `gpt-6.1-luna`, the
   cheapest model, on the CPU queue example with the `mpmc` task, one or two
   rounds. Unenforced: cost discipline is judgment; the user's grant is the gate.

Never write a host name, server name, absolute path or user name into anything
you commit (a fixture, a test, a doc, a PR body, an issue): say "a remote host".
Read [references/fixtures-and-checks.md](references/fixtures-and-checks.md) for how the
fixture page is built and how to add a scenario.

## Look at the screens on the fixture page

From the clients workspace root, build the page and serve its folder with any static server:

```bash
pnpm --filter @vibesys/desktop build:fixture
cd apps/desktop/dist/fixture && python3 -m http.server 4173
```

Open `http://localhost:4173/fixture.html?scenario=<seed>-<subagents>-<live|done>`
(default `5-4-live`). It is the same screens and the same session as the app, with a fake
daemon in place of the host, so a Start run, a message and a Stop all answer. It is a browser
page, not Electron: drive it with Playwright or any browser, and never launch the desktop app
for it. The page has no automated layout or wording check today; the critic step and the
independent review below are the checks. When a rule can be decided by a page (text clipped,
a raw reason shown), add it as a check by following `enforce-rules`.

## Produce the contact sheet

Take one screenshot per screen of the journey (runs list, Start run refused, a run's agent map,
an agent, a message sent, Stop) from the fixture page, cropped to the app window, and lay them
on one `contact-sheet.png`. The shots are the app window only, never the desktop or a full
screen: a full-screen capture shows the user's other windows and is never attached.

Make one sheet on the base branch before your change and one after, so a
reviewer sees what moved. Put both in the PR's Screens field. GitHub has no
command-line image upload: link a build artifact or a page the reviewer can
open, or give the file's location and ask the person to drag it into the PR.
Unenforced: no check reads the field; the PR template asks for it.

## Feel and alignment pass

Run it on the built app and on the fixture page, with a script (Playwright's
Electron launch for the app, a browser for the fixture page), and read numbers,
not impressions:

- **Feel.** Install a `PerformanceObserver` for `longtask` and `layout-shift`, press every
  control of the journey, and time each press to the second animation frame after it. A press
  answers within two frames, a long task (over 50 ms) belongs to start-up and never to a press,
  and no press moves the layout. Record the window with `recordVideo`, never the screen.
- **Alignment.** At a narrow (640 px), a default (1280 px) and a wide (1760 px) window, on a
  fixture with long names and empty values, read the bounding boxes: the title, the lists and
  the cards share their left edge, header cells and cells share theirs, rows are evenly spaced,
  numbers are right-aligned in tabular figures, and the page never scrolls sideways. A name a
  person chose is cut with an ellipsis and its whole text on hover; a word of ours wraps.
- **Themes.** Every theme, in its mode, and the operating system's mode flipped live.

Wait for the page's own states (`document.getAnimations()` for the finite animations, never
the infinite ones), never for a duration. Unenforced: the script is run by the author.

## Judge the screens (the critic step)

Before opening the PR, open every screen of the sheet that your change can
reach, and judge each against
[references/ux-principles.md](references/ux-principles.md) and the layout laws of
[references/visual-spec.md](references/visual-spec.md). Ask of each:

- What does a user learn in three seconds? Is it the thing that matters?
- What is unexplained: a color, a number, a word, an arrow?
- Would the user want this, or would they flag it?

Fix every problem you rate medium or higher before the PR opens; list the low
ones in the PR body. Unenforced: a judgment of what a user wants cannot be a check.

When the user reports a new complaint, add it to the principles, make it a check
when a page can decide it (follow `enforce-rules`), and add the screen to the
calibration set.

## Independent review

The builder is the worst judge of a screen: it already knows what each thing
means. For every new or reworked screen, and for every layout change a user can
see, a fresh agent that did not build the screen judges real-size screenshots
against [references/visual-review.md](references/visual-review.md). Give it what
that file lists and nothing else. Fix what it rates medium or higher.
Unenforced: no check can tell a fresh agent from the builder.

After changing the laws, the checks or the rubric, recalibrate as
[references/visual-calibration.md](references/visual-calibration.md) says: the
checks plus a blind reviewer must catch every complaint in the set.
Unenforced: the reviewer's half is a judgment run by an agent, not by CI.

## Steps in a PR

1. Rebase on the base branch (others edit the Run screen at the same time).
2. Pass `pnpm --filter @vibesys/desktop check` and `test`.
3. Produce the before and after sheets; run the critic step and, when the change
   is visible, the independent review.
4. Run `./scripts/check_clients.sh` (CI's client checks) before pushing.
5. Fill the Screens field of the PR template.
