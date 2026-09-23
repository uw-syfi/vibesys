# vs-project

One filesystem boundary for a repository-native VibeSys project.

`Project` binds human-authored task definitions and generated state to one
validated repository root. Application code constructs one project and uses
task operations directly and persistence through `project.state`:

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
