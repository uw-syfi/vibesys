# vs-project

## Responsibility

This package owns the `.vibesys` filesystem contract for a repository-native
project. It discovers and validates tasks, binds project state to one repository
root, and provides access to generated run state. Applications should use one
`Project` per root rather than assemble paths or state stores themselves.

Orchestration-specific settings, resume rules, agent roles, and round-history
interpretation belong in `src/vibesys/loops/agent` or
`src/vibesys/orchestration`, not in `vs_project`. New run manifests use a
versioned `OrchestrationDescriptor`; this package validates its portable JSON
envelope, while the owning orchestration validates the options and decides
whether a resumed run may change them. The version 3 loop configuration types,
resume comparator, and agent round methods remain available for old runs but
are deprecated for new code. No deprecation warning is emitted when reading
or resuming an old run.

## Usage

Application code uses task operations directly and persists state through
`project.state`:

```python
from vs_project.api import Project

project = Project.open(".")
task = project.select_task("latency")
manifest = project.state.load_project()
```

`Project.discover()` searches an existing path and its parents for the closest
`.vibesys/tasks` directory. `Project.open()` accepts any existing directory so
legacy inputs can use generated state without defining repository-native tasks.

The package owns the complete `.vibesys` filesystem contract:

```text
.vibesys/
├── tasks/<task-name>/
│   ├── OBJECTIVE.md
│   └── vibesys.input.toml
└── state/
    ├── project.json
    ├── runs/<run-id>/
    └── local/
```

Layout validation and persistence remain separate internal implementations.
The public package does not expose independently constructible layout or state
objects, which prevents application code from binding them to different roots.
State namespaces and immutable state value types remain public for integrations
that consume them.
