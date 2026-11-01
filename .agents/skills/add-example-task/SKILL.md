---
name: add-example-task
description: Add or change a runnable example project, a task bundle inside one (objective, input manifest, accuracy checker, benchmark, reference data), or an evaluator, task-local or a shared evaluator package built on the evaluator SDK. Use when adding or changing an example, the example registry, example tests, an evaluator package, the evaluator result protocol, or a task manifest's evaluator, accuracy, benchmark, profile, resources or environment tables.
metadata:
  kind: codebase
---

# Add an example task

Terms used below:

- **Project**: a candidate repository VibeSys optimizes. An **example** is a
  project kept in this repository, listed in the example registry.
- **Task** (or task bundle): one optimization problem in that project, in the
  project's `.vibesys/tasks/<task>/` directory: `OBJECTIVE.md` (goal and rules
  agents read), `vibesys.input.toml` (the manifest), and the files its commands
  use (accuracy checker, benchmark, optional `reference/` and `Dockerfile`).
  Variants sharing one codebase are one project with one task each.
- **Evaluator**: the programs that judge a candidate. The **accuracy** command
  passes or fails it; the **benchmark** command measures it. An evaluator is
  task-local (commands under the task directory) or a shared **evaluator
  package**, requested by exact name and version and shipped inside the
  VibeSys wheel.
- **Trusted inputs**: files the evaluator reads. Agents must not be able to
  edit them, or a candidate could grade itself.

## Builds on

- `software-design`: parse at the boundary; the manifest and the evaluator
  record stream are closed, validated contracts.
- `testing`: public-API tests, property tests, no real time in deterministic
  tiers. Also load `write-vibesys-tests` for test placement and Fakes.

## When this applies

- A new or changed example project, or an entry in the example registry.
- Any file in an example's `.vibesys/tasks/<task>/` directory.
- A shared evaluator package, or the evaluator result protocol, its SDKs and
  the reader in `vs-evaluator-protocol`.
- Not the domain prompt packs: load `write-agent-prompt`.

## Steps

1. **Lay out the project and register it.** Put tasks only in
   `.vibesys/tasks/<task>/`; a root `vibesys.input.toml` is rejected
   (`test_a_root_manifest_is_a_configuration_error_naming_the_task_location`).
   Add a `[[example]]` entry to the example registry;
   `test_every_example_directory_is_registered` prints the entry to add. A
   submodule under an example category's `repositories/` directory needs
   `external_repo = true`
   (`test_submodule_examples_are_registered_as_external_repos`). The registry
   fields are documented in [examples](../../../docs/contributing/examples.md).
2. **Write the manifest against its model.** The input manifest model is the
   schema; unknown keys fail. Each command is either a task-owned `command`
   argv or an `entrypoint` of a packaged `[evaluator]` plus `args`, never both.
   Pick one benchmark result contract: `[benchmark.result]` scrapes one JSON
   field, or `result_protocol = 2` makes VibeSys pass `--vs-output <path>` and
   read a validated record stream. Run `vibesys validate <project> --task <task>`;
   `test_example_validates` runs the same entry point for every registered task.
3. **Keep every file a command reads under the task directory.** `.vibesys/`
   is read-only to agents, so paths written `${PROJECT_ROOT}/.vibesys/tasks/<task>/...`
   are protected; a checker at the project root is not
   (`test_task_inputs_are_read_only_for_the_agent`). Every `${PROJECT_ROOT}/...`
   path in a command must exist (`test_command_path_references_exist`).
4. **Put example-specific rules in `OBJECTIVE.md`.** Trusted evaluation checks
   only accuracy and speed; the judge rejects a candidate that breaks any other
   rule the objective states, so write candidate rules there (where the engine
   lives, what may not be delegated, precision limits). Text true of a whole
   domain goes in the domain pack instead (`write-agent-prompt`).
   Unenforced: telling task text from domain text needs a reader.
5. **Declare what the run needs, not where it runs.** `[agent] domain` names a
   registered domain. `[resources]` is a portable compute request: counts and
   an accelerator backend, never a provider, partition or image; SkyPilot runs
   require it. A file named `Dockerfile` in the task directory selects the
   Docker run environment, so name a service image file anything else
   (`test_train_ticket_assets_build_current_java_modules`). Model weights come
   from the task's reference model directory or reference metadata file
   (`test_remote_environment_prepares_weights_without_a_host_copy`).
6. **Build a shared evaluator only for reuse across tasks.** Follow
   [references/evaluators.md](references/evaluators.md) for the package
   manifest, the result protocol, failure attribution and the Go SDK. A
   package's metadata is validated whenever any packaged task resolves, so one
   broken package fails every packaged example (`test_example_validates`).
7. **Try it from the catalog, never in place.** `vibesys example new <example> <dest>`
   copies it into a fresh repository
   (`test_new_copies_the_example_commits_it_and_prints_the_run_line`);
   `vibesys example` imports no run engine
   (`test_light_entrypoint_imports_only_light_modules`).

## Testing

- The registry tests cover structure for every task with no Docker, GPU or
  weights; needing those never exempts an example. A static check that fails
  today goes in the entry's `known_failing`, a strict xfail
  (`test_registered_tasks_match_disk`).
- Test example-specific logic in the examples test tier, loading the task
  through its shared task-loading helper. These run in the deterministic tier:
  a new real subprocess, thread or clock fails
  `test_deterministic_tests_use_no_real_time_threads_processes_or_sockets`.
- A test that compiles a candidate and runs a real evaluator on it carries the
  `native_build` marker, which the default run deselects; add its file to the
  native-evaluator test group of the repoctl check configuration. Unenforced:
  mapping arbitrary check-group command lines to test files would breed false
  positives.
- Test a Go or Rust evaluator package with `go test` or `cargo test` beside its
  code; CI runs it in the native job when the package changes.
  Unenforced: the workflow's path filter is configuration, not a test target.
- A test file inside an example's `.vibesys/` directory is outside pytest's
  `testpaths` and runs only if a group in the repoctl check configuration names
  it; add it to the examples test group or move the test into the examples
  test tier. Unenforced: same reason as above.
- After renaming or deleting an example, every reference to its path in docs,
  workflows and scripts must still resolve
  (`test_example_paths_referenced_outside_examples_exist`).

## Known failure modes

- A task with neither a reference model nor reference metadata failed at startup in
  every run environment
  (`test_remote_environment_prepares_weights_without_a_host_copy`).
- An evaluator tool's native build needs worked only on base images that
  happened to ship them; declare them as `system_packages`
  (`test_container_setup_installs_every_system_package_a_bundled_tool_declares`).
- A task-level `Dockerfile` that built a service image silently selected the
  Docker run environment and broke the documented launch
  (`test_train_ticket_assets_build_current_java_modules`).
- Benchmark workers that start measuring before all are ready report a skewed
  or partial-mix sample; rendezvous before the measurement starts.
  Unenforced: a property of each benchmark's design; no check names it.
- Example-specific contract text in a shared prompt reached runs of other
  examples; it belongs in that example's `OBJECTIVE.md`. Unenforced: needs a
  reader, see `write-agent-prompt`.
- A candidate broke a rule the objective implied but did not state; state
  every rule. Unenforced: completeness needs a reader.
- Test files inside some examples' `.vibesys/` directories are named by no
  check group, so CI never runs them. Unenforced: mapping arbitrary check-group
  command lines to the files they collect would breed false positives.
- A local `replace` in an evaluator's `go.mod` builds in CI and fails where
  VibeSys runs the package (`test_evaluator_module_has_no_local_replace`).

## Sources of truth

- The input manifest model in the VibeSys inputs layer: the task manifest
  schema and how commands resolve.
- The example registry and its catalog listing, documented in
  [examples](../../../docs/contributing/examples.md).
- [running-vibesys](../../../docs/running-vibesys.md): task layout, task
  Dockerfiles, `vibesys example`.
- The evaluator result protocol document shipped with the evaluator SDK.
- The failure-classification table in `vs-runtime`: whose fault a failed stage is.
