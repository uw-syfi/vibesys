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

## What New run shows about a task

New run offers a task by its domain, served model, hardware and workload, read
from the `[catalogue]` table of its `vibesys.input.toml`, never from a directory
name: `domain` (`llm-serving`, `data-structures`, `databases` or
`microservices`), `[catalogue.hardware]` (`accelerator`, `count`, `vendor`),
`[catalogue.model]` (`name`, `maker`; required for `llm-serving`) and
`[catalogue.workload]` (`name`, `description`; required for `data-structures`).
The compute backend is the first of the file's `backends`. The objectives come
from the task's `objectives.toml` (every in-tree task declares at least one) and the goal from its
`OBJECTIVE.md`, cut at 32,000 characters. `vibesys example list --json` and the
helper's `examples.list` (with `with_catalogue`, feature `example_catalogue`)
carry them as each example's `task_details`. A task of an `external_repo`
example declares its table in that repository and is listed without details
until it does.

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
| `candidate-build` | A task whose evaluator package requires candidate outputs (`candidate_outputs`) but declares no `[build]`, so every measurement fails its check (`InputBundle.missing_build`, the rule New run preflight applies too) | Example, task, the missing outputs |
| `backend` | A task that declares no `backends`, so a run would fall back to the CLI's `cuda` default and fail a CPU task on a CPU-only host | Example, task |
| `catalogue` | A task of an in-tree example with no `[catalogue]` table in its `vibesys.input.toml`, or an LLM-serving task whose table has no model and maker (see below) | Example, task |
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
