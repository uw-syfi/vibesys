# Fixtures and screen checks

How the pieces fit, and how to extend them. `@vibesys/run-fixtures` owns the
fixtures; the end-to-end suite of `@vibesys/app-desktop-shell` replays them and
runs the checks.

## A fixture

A fixture is a run's events as the run protocol delivers them, with how to play
them back: live or finished, where a live stream starts, where it drops (the
client reconnects and resubscribes from the last sequence it saw), and how a
finished run ended. `parseFixture` names the key or path it refuses: an unknown
key, an event the protocol rejects, a sequence that does not increase.

The committed set covers long titles, every failure reason, retries,
continuation rounds, ideas on non-input bases, a live run with a reconnect, a
finished run, an agent turn at realistic size (`agent-turn-rich`: long reasoning, thirty
tool calls, a failed command, an edit, a Markdown answer, and a turn still thinking), the
desktop shell's fake run, and 600 ideas over 14 days. `fixtureNamed` gives one by name.

## Add a case

- **A synthetic case** (the usual one): add a scenario to the package's
  `scenarios`, built with its trace helpers (`addIdea` writes an idea's
  workstream, turn and gate; `turnTranscript` and `toolCall` write a turn's
  text). Then `pnpm --filter @vibesys/app-desktop-shell fixtures` rewrites the
  committed data, and the package's tests hold the data equal to the generators.
- **A real run**: copy its event journal from where the run was made and run
  `pnpm --filter @vibesys/app-desktop-shell record` with the journal, a name, a
  description and the state. The recorder removes host names, absolute paths,
  user names, e-mail and IP addresses and URL hosts, and refuses to write a
  fixture in which one survives. Add `--user` and `--host` for the names of the
  machine the run was made on. Read the result before committing it.
- Add the fixture to the screen matrix in the suite when its case is a layout
  one (a title, a count, a state).

## Add or change a check

A check is a function in the suite's in-page module: it reads the live DOM and
returns violations; it never changes the page. It must fail on a known-bad
page: add the bad screen to the suite's known-bad pages with the message the
check gives, and keep the clean page passing every check. A check that stops
looking, or that cries wolf, fails its own test.

Checks are deterministic: the viewport, theme and fonts are fixed, and each
check waits for the screen to settle (fonts loaded, layout unchanged for several
frames), never for a duration. Do not add a sleep to make a check pass.

## Known failures

A defect already on the base branch that no change of yours fixes goes in the
known-failure list with the issue that tracks it, naming the suite, the check,
the text and the screens it shows on. It is a debt, not a skip: the suite that
owns the entry fails on any new violation and on a listed one its screens no
longer show. Name the mechanism in the entry's text, not one screen, so that a
fix deletes one entry. A failing screen is held by the first entry that matches.

## Screen suites and journeys

A screen suite owns the look of its screens: it walks them at each width and asks
for every check, the visual spec's laws included, against the known failures. A
journey proves behavior and asks for the defect checks alone: a journey that
passes through a screen does not own how that screen is laid out, and the laws
would fail it for a debt it cannot pay.
