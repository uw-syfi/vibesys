# Agent skills

Skills for agents developing VibeSys. `.claude/skills` is a symlink to this
directory. These are not the bundled product skills, which ship to
optimization agents. The convention for writing and changing skills here is
the [write-skills](write-skills/SKILL.md) skill, and the `check_skills` check
enforces it, including that this index lists every skill under its kind.

## Generic

Engineering judgment that holds in any well-run codebase.

- [software-design](software-design/SKILL.md): deep modules, declared
  interfaces, one-way dependencies, fitting a change to the design.
- [testing](testing/SKILL.md): public-API tests, Fakes instead of mocks,
  property-based tests, no flakiness.

## Codebase

How to make one kind of change in this repository.

- [add-agent-provider](add-agent-provider/SKILL.md): add or change an agent CLI
  provider, upgrade agentshim, or bump a pinned CLI version.
- [add-bundled-skill](add-bundled-skill/SKILL.md): add or change a bundled
  product skill, its sidecar selection rules, platform references or its
  vendoring update script.
- [build-client-feature](build-client-feature/SKILL.md): add or change a
  feature in the TypeScript clients: package placement, machines and folds,
  run-session wiring, cancellation and bounded waits.
- [change-container-images](change-container-images/SKILL.md): change the
  agent container image or a Dev Container Feature (toolchains, container
  runtime, pip extras) and the mechanism that builds it.
- [change-module-graph](change-module-graph/SKILL.md): add, split or connect
  modules, libraries and packages, and add I/O interfaces and implementations.
- [enforce-rules](enforce-rules/SKILL.md): enforce a rule with the strongest
  mechanism that holds it, from types and lint rules to marked prose.
- [write-agent-prompt](write-agent-prompt/SKILL.md): write or change the text
  sent to agents at the layer that owns it, with its checks and goldens.
- [write-vibesys-tests](write-vibesys-tests/SKILL.md): this repository's test
  harness: tiers, vs-sim, depth, contract suites, faults, goldens, budgets.

## Workflow

Tasks around the code.

- [create-issue](create-issue/SKILL.md): investigate, draft and file issues.
- [open-pr](open-pr/SKILL.md): prepare and open pull requests, including stacks.
- [review-pr](review-pr/SKILL.md): review a pull request or local diff.
- [triage-prs](triage-prs/SKILL.md): triage open pull requests into a
  maintainer queue.
- [vs-meta-opt](vs-meta-opt/SKILL.md): run and audit meta-optimization campaigns.
- [verify-desktop-ui](verify-desktop-ui/SKILL.md): verify a desktop UI change before
  its PR opens: fixtures, screen checks, contact sheet, UX critic.
- [write-docs](write-docs/SKILL.md): decide what belongs in docs, write it in
  the house style, and move or delete pages safely.
- [write-skills](write-skills/SKILL.md): the convention for skills in this
  directory, and how to apply it.
