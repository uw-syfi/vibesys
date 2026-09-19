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

## Two axes

**Static checks** (structure and configuration) run for every entry and every
task, always. Needing docker, a cluster, a GPU, or model weights never exempts
an example: none of them is needed to validate a manifest.

**Live coverage** is the `live` field (`none`, `manual`, `ci`): whether any
real run exercises the example. It never changes which static checks run.
`requires` documents what a live run needs.

## Entry fields

| Field | Meaning |
| --- | --- |
| `path` | Repo-relative example root. |
| `layout` | `task` or `legacy`. |
| `tasks` | `"all"` (default) or the exact task names on disk. Omit for legacy. |
| `live` | `none`, `manual`, or `ci`. |
| `requires` | `overlay`, `docker`, `kubernetes`, `gpu`, `model-weights` (documentation). |
| `known_failing` | `[{ check, reason, tracking }]`: a static check that fails today. |
| `skips` | `[{ check, reason }]`: a check that cannot run because source is absent from the checkout. |

## Static checks

| Check | Catches | Fails as |
| --- | --- | --- |
| Registry completeness | New example directory, missing path, wrong layout, task list out of date | Names the file and the entry to add |
| `validate` | Everything `vibesys validate` rejects, per task, both layouts | Example path plus task |
| `path-refs` | `${PROJECT_ROOT}/...` in accuracy/benchmark commands pointing at nothing | Example, task, path |
| `trust-policy` | Files those commands read that the agent could edit, using `build_project_path_policy` on a scratch copy (both layouts) | Lists the writable files and the read-only set |
| Stale references (repo-wide) | `examples/...` literals in workflows, scripts, docs, and READMEs whose target is gone | File and literal; exclusions live in the test with a comment |

`known_failing` is a strict xfail: when the check starts passing the test fails
until you remove the entry, so the list only shrinks. `skips` is the only way
to omit a check for one example, and is also strict: the test fails if the skip
is no longer needed. Today one skip exists (`path-refs` for the
deathstarbench overlay, whose checkout has no candidate source).

Overlays are fetched in CI. A missing overlay fails there
(`VIBESYS_REQUIRE_EXAMPLE_OVERLAYS=1`) and skips only locally; run
`uv run python scripts/example_repositories.py` first.

Not covered: running an evaluator, and files a command reads indirectly
(imports, config it loads from disk).
