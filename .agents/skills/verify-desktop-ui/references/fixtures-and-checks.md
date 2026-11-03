# The fixture page

How the pieces fit, and how to extend them. The desktop app's `testing` folder owns the
fixture page and the Fakes it runs on.

## What it is

The fixture page renders the app's screens (`ConnectedApp`) over a `FakeDaemon` through a fake
transport, with the real `makeHostSession`. `vite build --mode fixture` (the app's `build:fixture`
script) builds it to a static page. A scenario is `generateScenario({seed, subagents,
live})`: a seeded run with a lead, subagents, their harnesses, timelines and handoffs, live or
finished. The page's `scenario` query parameter, `<seed>-<subagents>-<live|done>`, picks one.

## Add a case

Add the state to the scenario generator in the `testing` folder's orchestrator Fakes, not to the
page: the same generator feeds the unit and property tests, so a case the page shows is one the
tests can fold. Keep names, paths and hosts generic (`host-a`).

## Known failures

Today nothing checks the page's layout or wording automatically, so there is no known-failure
list. A defect already on the base branch that no change of yours fixes goes in an issue, named
in the PR's Screens field.
