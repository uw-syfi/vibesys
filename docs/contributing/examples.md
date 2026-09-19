# Adding an example

Every runnable example under `examples/` is declared in
[`examples/registry.toml`](https://github.com/uw-syfi/vibesys/blob/main/examples/registry.toml).
CI iterates that file, not a glob, so an example that is not registered fails
`tests/architecture/test_example_registry.py` (the failure prints the entry to
add).

An example is a directory with a root `vibesys.input.toml` or `OBJECTIVE.md`
(legacy layout), or with `.vibesys/tasks/<task>/` (task layout). Submodule
examples under `examples/<family>/repositories/` are registered with
`requires = ["overlay"]`. Baselines, starters, and evaluator sources are not
examples and are not registered.

## Entry fields

| Field | Meaning |
| --- | --- |
| `path` | Repo-relative example root. |
| `layout` | `task` or `legacy`. |
| `tasks` | `"all"` (default) or the exact task names on disk. Omit for legacy. |
| `requires` | `overlay`, `docker`, `kubernetes`, `gpu`, `model-weights`. |
| `status` | `validated`, `live-only`, or `known-failing`. |
| `reason`, `tracking` | Required for `known-failing` (with a PR or issue link); `reason` for `live-only`. |
| `failing_checks` | `validate` or `trust-policy`; `known-failing` only. |

`validated` may only require `overlay`, which the `validate-examples` job
fetches. Anything needing docker, a cluster, a GPU, or weights is `live-only`.

Status never turns a static check off. `validated` and `live-only` entries get
exactly the same checks below; `live-only` only records that a live run is not
covered because CI lacks something the example needs. `known-failing` runs the
same checks and expects the listed ones to fail. The only thing that stops a
static check is an overlay that is not fetched, which fails in CI and skips
locally. Two limits apply by layout: trust policy runs only for `task`
layout (legacy inputs use a fixed trusted list), and path references are
skipped for `overlay` examples (the checkout has no candidate source).

## What CI checks for each entry

| Check | Catches | Fails as |
| --- | --- | --- |
| Registry completeness | New example directory, missing path, wrong layout, task list out of date | Names the file and the entry to add |
| Validate | Everything `vibesys validate` rejects, per task, both layouts | Message with the example path and task |
| Path references | `${PROJECT_ROOT}/...` in accuracy/benchmark commands pointing at nothing (skipped for overlays, which carry no candidate source) | Example, task, and path |
| Trust policy (task layout) | Files those commands read that the agent could edit, using `build_project_path_policy` on a scratch copy | Lists the writable files and the read-only set |
| Stale references | `examples/...` literals in workflows, scripts, docs, and READMEs whose target is gone | File and literal; exclusions live in the test with a comment |

A `known-failing` check is strict xfail: when it starts passing the test fails
until you remove the entry, so the list only shrinks. In CI a missing overlay
fails (`VIBESYS_REQUIRE_EXAMPLE_OVERLAYS=1`); locally it skips, so run
`uv run python scripts/example_repositories.py` first.

Not covered: running an evaluator, docker, Kubernetes, GPUs, model weights, and
files a command reads indirectly (imports, config it loads from disk).
