# Adding an example

Every runnable example under `examples/` is declared in
[`examples/registry.toml`](https://github.com/uw-syfi/vibesys/blob/main/examples/registry.toml).
CI iterates that file, not a glob, so an example that is not registered fails
`tests/examples/test_example_registry.py` (the failure prints the entry to
add).

An example is a project root under `examples/<category>/` with its tasks in
`.vibesys/tasks/<task>/` (the only layout; a root `vibesys.input.toml` fails the
registry and `vibesys validate`). Variants that share one candidate codebase are
one project with one task per variant. Submodule examples under
`examples/<category>/repositories/` are registered with `external_repo = true`.
Baselines are not examples and are not registered; shared evaluators are
versioned packages under `resources/evaluators/`. Users start a run from an
example with `vibesys example new` ([Running VibeSys](../running-vibesys.md)).

## Task metadata and objectives

In-tree examples declare `[catalogue]` metadata in `vibesys.input.toml`.
The manifest reader validates its domain, hardware, model and workload fields.
This metadata remains part of the task format; the retired desktop helper no
longer exposes it as `task_details` or `examples.list`.

The Python run CLI reads `objectives.toml` to select the metric axes and their
`max` or `min` directions. Every in-tree task declares at least one objective.
`OBJECTIVE.md` holds the task's goal. These files are validated independently
of the example listing.

`vibesys example list` prints categories, example names and task names as text.
The legacy `example list --json` command is removed. Use `vibesys example new`
to create a project, as described in [Running VibeSys](../running-vibesys.md).

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
| `tasks` | `"all"` (default) or the exact task names on disk. |
| `external_repo` | `true` when the task files and app source come from another repository that `scripts/example_repositories.py` checks out. Default `false`. It is the only field that changes behavior. |
| `known_failing` | `[{ check, reason, tracking }]`: a static check that fails today. |
| `skips` | `[{ check, reason }]`: a check that cannot run because source is absent from the checkout. |

## Static checks

| Check | Catches | Fails as |
| --- | --- | --- |
| Registry completeness | New example directory, missing path, root `vibesys.input.toml`, task list out of date | Names the file and the entry to add |
| `validate` | Everything `vibesys validate` rejects, per task | Example path plus task |
| `path-refs` | `${PROJECT_ROOT}/...` in check/measure (accuracy/benchmark) commands pointing at nothing | Example, task, path |
| `trust-policy` | Files those commands read that the agent could edit, using `build_project_path_policy` on a scratch copy | Lists the writable files and the read-only set |
| `candidate-build` | A task whose evaluator package requires candidate outputs (`candidate_outputs`) but declares no `[build]`, so every measurement fails its check (`InputBundle.missing_build`, validated by the task input loader) | Example, task, the missing outputs |
| `backend` | A task that declares no `backends`, so a run would fall back to the CLI's `cuda` default and fail a CPU task on a CPU-only host | Example, task |
| `catalogue` | Missing or invalid catalogue metadata in an in-tree task, checked through the manifest reader | Example, task |
| Objective direction | An in-tree task with no metric axis or an invalid `max`/`min` direction in `objectives.toml` | Example, task |
| `candidate-import` | A task script that does `from main import ...` and does not put the project root on `sys.path` itself. Running `python .vibesys/tasks/<task>/<dir>/checker.py` puts the script's own directory there, not the root where the candidate's `main.py` lives | Script, and the line to add |
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
(config it loads from disk, imports other than the candidate's `main`).
