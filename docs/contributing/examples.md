# Adding an example

Every runnable example under `examples/` is declared in
[`examples/registry.toml`](https://github.com/uw-syfi/vibesys/blob/main/examples/registry.toml).
CI iterates that file, not a glob, so an example that is not registered fails
`tests/examples/test_example_registry.py` (the failure prints the entry to
add).

An example is a directory with a root `vibesys.input.toml` or `OBJECTIVE.md`
(legacy layout), or with `.vibesys/tasks/<task>/` (task layout). Submodule
examples under `examples/<family>/repositories/` are registered with
`external_repo = true`. Baselines, starters, and evaluator sources are not
examples and are not registered.

## Static checks run for every example

Static checks (structure and configuration) run for every registered example
and every task, always. Needing docker, a cluster, a GPU, or model weights
never exempts an example: none of them is needed to validate a manifest. There
is no live-coverage tracking in the registry; add a field for it only when a
check consumes it.

## Entry fields

| Field | Meaning |
| --- | --- |
| `path` | Repo-relative example root. |
| `layout` | `task` or `legacy`. |
| `tasks` | `"all"` (default) or the exact task names on disk. Omit for legacy. |
| `external_repo` | `true` when the task files and app source come from another repository that `scripts/example_repositories.py` checks out. Default `false`. It is the only field that changes behavior. |
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
deathstarbench external repo, whose checkout has no candidate source).

External repos are fetched in CI. A missing checkout fails there
(`VIBESYS_REQUIRE_EXAMPLE_EXTERNAL_REPOS=1`) and skips only locally; run
`uv run python scripts/example_repositories.py` first.

Not covered: running an evaluator, and files a command reads indirectly
(imports, config it loads from disk).
