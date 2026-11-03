# Development guide

Start with [Architecture](architecture.md) for process ownership, the
[Python module graph](python-module-graph.md) for Python boundaries and
[Clients architecture](clients-architecture.md) for the desktop's state and
runtime boundaries. Component READMEs hold build and command details; repository
skills hold the workflow for making each kind of change.

## Before you change code

Use the repository software-design skill for code changes and the testing skills
for tests. Keep policy with its owner, dependencies one-way and I/O behind the
owning library's interface. Product policy belongs above reusable mechanisms.
The [coding guide](coding-best-practices.md) describes the enforced limits.

## Local development

### Check your environment

Run the repository doctor's environment check before the first check on a
machine. It checks tool versions, dependencies, temporary storage and hook setup.
Apply its reported environment exports in the same shell that runs later checks.
A setup failure should be repaired at that boundary rather than suppressed in
a test or gate.

The [repository tooling README](https://github.com/uw-syfi/vibesys/blob/main/support/repoctl/README.md)
owns setup, development launchers and check commands. Python dependencies use
uv, client dependencies use the pinned workspace package manager, and Go tools
use the repository's declared toolchain. The client test runner checks its
pinned runtime before running tests.

### Work from a source checkout

Keep credentials and machine-specific configuration outside tracked changes.
Use generic placeholders in examples and prose. Start the desktop through the
repository development launcher so the app and host artifacts agree; see
[Desktop app development](desktop-development.md).

Submodules are opt-in. Fetch only what a task needs. Missing example checkouts
do not prevent ordinary unit tests; dedicated example validation covers those
contracts. Review and stage explicit paths so a local submodule checkout never
silently changes the repository's pin.

## Verification tiers

Use the cheapest check that can disprove the change first. Types, dependency
checks and pure tests cover local contracts. Deterministic simulation exercises
whole lifecycles with injected I/O and a virtual clock. Real-system tiers check
boundaries that simulation cannot establish.

A deterministic test uses no real clock, process, thread, socket or signal
outside the simulation library. Fakes stand at I/O boundaries; tests exercise
public APIs. Timing bounds guard against hangs and never decide correctness.
See [Testing map](testing-map.md) for the suite boundaries.

### Dynamic-loop smoke tier

The smoke tier runs a complete search with scripted agents and simulated
infrastructure. It checks the connection between policy, lifecycle, evidence
and final selection without model-provider cost.

### Agent-less runs

Scripted agents make search behavior repeatable. They are useful for lifecycle
and recovery assertions, but do not establish that a real provider accepts the
launch configuration or that a model can solve the task.

### Real-cluster tier

Real-cluster checks exercise dispatch, collection and cleanup on external
infrastructure. They need operator credentials and resources and belong outside
the deterministic suite. See [remote Slurm execution](../remote-slurm-execution.md)
for the operator model.

### Minimal-container tier

The container tier checks the built agent image, installed provider programs,
tool-server startup and confinement wiring. It complements simulation by
checking the actual process boundary. It does not establish device-driver or
remote-cluster compatibility.

## Extend VibeSys

A search is written as a journaled flow; see [Flows](flows.md). The
[orchestration guide](orchestration-runtime.md) explains policy ownership.
Domain resources, [input bundles](examples.md), [profilers](extending-profilers.md)
and [skills](skill-metadata.md) each have a focused guide.

## CI gates

CI selects checks from the repository's component and dependency policy.
Unowned changes fail selection. The local test command runs the groups selected
for the change and reports groups that cannot run locally with their reasons.
Inspect the plan when the selection is surprising rather than guessing a suite.

The check registry owns command lists, selection rules and budgets. This guide
does not duplicate them. The [tooling README](https://github.com/uw-syfi/vibesys/blob/main/support/repoctl/README.md)
explains how to inspect and run the selection.

### Client checks before a push

Run the repository client-check command for desktop or shared client changes.
Package tests alone miss dependency, runtime, unused-code and design-system
violations. Visual checks use the built app and deterministic fixtures; the
repository desktop verification skill owns their review workflow.

### Pre-push gate

Every push runs the selected fast checks through the tracked hook. Verify that
the hook is installed; the doctor reports a different hook configuration.
Repair setup failures and rerun the gate before pushing. CI and the merge queue
remain authoritative for checks that need their infrastructure.

### Private terms

Private server names, internal hostnames, personal paths and credentials do not
belong in commits. The local private-terms check reads an external private list
and reports locations without printing the matched secrets. A machine without
that list cannot establish that a change contains no private terms. Review is
still necessary because CI cannot carry the private list.

### Format

Run the repository format check. Generated artifacts are regenerated from their
source rather than hand-formatted independently.

### Lint

Run the repository lint check. A suppression is an explicit opt-out with a
reason and a waiver, not a fix for a boundary violation. See
[coding best practices](coding-best-practices.md#ratchets).

### Coverage

Coverage gates catch untested changes and weakly covered modules. The repository
configuration owns the thresholds and exceptions. A coverage number does not
replace correctness properties: test public behavior, recovery and failure
contracts with the smallest relevant suite.
