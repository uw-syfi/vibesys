---
name: verify-desktop-ui
description: Verify a desktop UI change the way a user sees it, before its pull request opens: replay run fixtures through the built app, pass the layout and wording checks on every screen, produce the app-window contact sheet, judge each screen against the UX principles and the visual spec, and have an independent reviewer judge it. Use when changing the desktop app's screens (the Run screen, sidebar, New run, settings), fixing a visual or wording bug there, adding a state a user can see, or opening a pull request that touches them.
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

1. **Fixtures** (seconds): recorded and synthetic runs replayed into the built
   app. Every UI change starts and usually ends here.
2. **Agent-less runs** (minutes): the real backend with scripted agents and no
   model calls, on the CPU queue example. From a project copied out of the
   `queue-rs` example, run `vibesys --task mpmc --outer-loop dynamic
   --agent-backend scripted --max-rounds 5 --max-in-flight 2`; in the desktop
   New run form, put the same flags in Advanced. The scripted agents' seed is the
   `scripted_seed` setting of the agent configuration, so a run repeats. Use them
   for what a fixture cannot show: startup, pause and resume, an ended run read
   from its history.
3. **Real runs with live agents**: only at the end of a batch of UI changes, or
   when the user explicitly grants one. Use Codex with `gpt-6.1-luna`, the
   cheapest model, on the CPU queue example with the `mpmc` task, one or two
   rounds. Unenforced: cost discipline is judgment; the user's grant is the gate.

Never write a host name, server name, absolute path or user name into anything
you commit (a fixture, a test, a doc, a PR body, an issue): say "a remote host".
`recordFixture` scrubs a recorded run and refuses a result that still leaks;
read [references/fixtures-and-checks.md](references/fixtures-and-checks.md) for
how to add a case.

## Launch the app on a fixture and run the checks

From the clients workspace root:

- `pnpm --filter @vibesys/app-desktop-shell test:e2e` builds the shell and runs
  every journey, including the screen checks over the whole matrix: run, idea,
  step and item levels, the Timeline and Agents faces and the scrubbed run;
  live and finished; sidebar shown and hidden; 1000, 1440 and 1920 px; and the
  stress fixtures (long titles, every failure reason, retries, rounds, bases that
  are not the input, a reconnect, 600 ideas over 14 days). It takes about a minute.
- A failure names the screen, the check, the offending text and its element.
  Fix the UI, not the check. A known-failure entry is only for a defect already
  on the base branch that has its own issue; never add one for a defect your
  change causes. The run fails on an entry no screen shows any more, so delete
  the entries your change fixes. CI runs the same suite (`clients_e2e`).
- To look at a fixture yourself, build the shell and run one spec headed:
  `pnpm --filter @vibesys/app-desktop-shell e2e --headed <spec> -g <fixture>`.
- The same suite in CI's Linux image (needs Docker):
  `pnpm --filter @vibesys/app-desktop-shell e2e:linux`. Run it when a check
  passes here and you suspect a font or rendering difference. The native
  replays are made on the host and mounted read-only; a spec that builds a
  fixture differing from its declaration needs one host run first.

## Iterate on a fixture with hot reload

To edit a screen and watch it change, serve the shell in a browser over a fixture instead of
rebuilding and rerunning the suite:

`pnpm --filter @vibesys/app-desktop-shell dev:fixture [NAME] [--port N] [--theme paper]`

It starts Vite's dev server (printing the URL) with the same Fake bridge and fixture replay the
e2e journeys use, opens the fixture's run from Recent, and plays a live fixture's later events.
Edits to feature or shell source update the page in place, with no reload. `NAME` is a fixture of
`@vibesys/run-fixtures` (default `weeks-600-ideas`; `--list` prints them). Visit
`/fixture/NAME[?theme=paper]` to switch fixture or theme without a restart. It is a browser page,
not Electron: drive it with Playwright or any browser, and never launch the desktop app for it.
Finish with the checks above, which this does not replace.

## Produce the contact sheet

`SCREENS_DIR=<dir> pnpm --filter @vibesys/app-desktop-shell e2e screens` writes
one screenshot per screen of the matrix and `contact-sheet.png` with all of
them. The shots are the app window only, never the desktop or a full screen: a
full-screen capture shows the user's other windows and is never attached.

Make one sheet on the base branch before your change and one after, so a
reviewer sees what moved. Put both in the PR's Screens field. GitHub has no
command-line image upload: link a build artifact or a page the reviewer can
open, or give the file's location and ask the person to drag it into the PR.
Unenforced: no check reads the field; the PR template asks for it.

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
2. Pass `pnpm --filter @vibesys/app-desktop-shell test:e2e`.
3. Produce the before and after sheets; run the critic step and, when the change
   is visible, the independent review.
4. Run `./scripts/check_clients.sh` (CI's client checks) before pushing.
5. Fill the Screens field of the PR template.
