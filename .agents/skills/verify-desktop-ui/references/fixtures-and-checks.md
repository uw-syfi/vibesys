# The fixture page

How the pieces fit, and how to extend them. The desktop app's `testing` folder owns the
fixture page and the Fakes it runs on.

## What it is

The fixture page renders the app's screens (`ConnectedApp`) over a `FakeDaemon` through a fake
transport, with the real `makeHostSession`. `vite build --mode fixture` (the app's `build:fixture`
script) builds it to a static page. A scenario is `generateScenario({seed, subagents,
live})`: a seeded run with a lead, subagents, their harnesses, timelines and handoffs, live or
finished. The page's `scenario` query parameter, `<seed>-<subagents>-<live|done>`, picks one.

## The three report cases

The run report must hold one layout from the first minute to day six, so it has three named worlds
(defined in the orchestrator Fakes, with the report cases). Each replaces the scenario, the search view and the
cost, and no other parameter changes them. Open the run on the Runs list to see its report:

| Case | Address on the fixture page | What it is |
| --- | --- | --- |
| Just started | `?case=started` | Running for about two minutes: one agent, nothing measured, no idea, no objective declared. |
| Failed | `?case=failed` | Failed within a minute and a half (lead lost), one objective with 3 measurements, one unpriced turn. |
| Six days | `?case=week` | Live for six days: two objectives with 400 points served of 3,000 (the host's cap, so the state is truncated), 64 ideas, workstreams, a lead report, a cost of $413. |

Check each at windows of 1000 (the columns stack), 1440 and 1920. The fixture page sets the
browser's clock to the scenario's "now" when it loads and lets it run on from there (it replaces
`Date.now` in the page only), so the ages the screens write are the scenario's, not the
distance to a fixed date. The same moment is in `document.documentElement.dataset.nowMs`. The
week case's lead report is very long, with an unbroken path in it, and some of its idea titles are
very long: the cut text, the ellipses and the "Read the whole report" link are on screen there. The
failed case's report is short with an empty field; the started case has none.

## The three Machines worlds

The Machines page is one table (a row for each machine, its places beneath it), so it has three named
worlds (defined in the desktop app's `testing` folder, with the cases of each page). Each names the hosts it shows,
replaces any `host` parameter, and is opened by the Machines entry of the sidebar:

| World | Address on the fixture page | What it is |
| --- | --- | --- |
| One machine | `?machines=one` | This machine, live, with CPU, memory and two GPUs, and four places (one unreachable). |
| Several | `?machines=several` | Five machines: live with stats, live with eight GPUs, reconnecting (its link drops after its first sample, so its readings dim), refused (no host program), and one with a 70-character alias whose GPU section has no tool; with places reachable and not, and one machine whose places could not be read. |
| Unsupported | `?machines=unsupported` | This machine, live, whose host refuses the stats topic: `No stats` with the reason on hover. |

Check each at windows of 1000, 1440 and 1920. Add machine opens its input; the fixture refuses the
add and the remove in words, because the hosts shown are fixed by the address.

## Add a case

Add the state to the scenario generator in the `testing` folder's orchestrator Fakes, not to the
page: the same generator feeds the unit and property tests, so a case the page shows is one the
tests can fold. Keep names, paths and hosts generic (`host-a`).

## Known failures

Today nothing checks the page's layout or wording automatically, so there is no known-failure
list. A defect already on the base branch that no change of yours fixes goes in an issue, named
in the PR's Screens field.
