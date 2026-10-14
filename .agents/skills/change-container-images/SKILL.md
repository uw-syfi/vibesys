---
name: change-container-images
description: Change the image every run's agent container starts from, by adding or changing a Dev Container Feature (the agent layer, a toolchain such as Rust or Go, the container runtime, pip extras) or the mechanism that builds it. Use when editing a Feature folder, the toolchain catalog, the Feature-to-Dockerfile generator or image builder, the agent image build, the in-container Python interpreter, provider CLI or Node pins, or the feature-tests and agent-image CI jobs.
metadata:
  kind: codebase
---

# Change container images

Every run's agent runs in a local Docker container (see "run environment" in
[sandboxing.md](../../../docs/contributing/sandboxing.md)). Its image is the
task image, or a backend base image, plus layers built from
[Dev Container Features](https://containers.dev/implementors/features/): a
Feature is a folder with `devcontainer-feature.json` (id, options,
environment) and an install script that runs as root during the build. VibeSys
does not run the `devcontainer` CLI at run time: it generates a Dockerfile from
the Features and runs plain `docker build`. The CLI only proves in CI that each
Feature conforms to the specification. The design and the Feature owners are in
[devcontainer-features.md](../../../docs/contributing/devcontainer-features.md).

## Builds on

- `software-design`: each Feature lives with the module that owns what it
  installs and reaches the mechanism as data; the mechanism imports no owner.
- `testing`: pure planning is property-tested; Docker sits behind an
  injected command runner; real builds belong to the e2e tier.

## When this applies

- Adding a CLI, system package or Python package to the agent layer, or
  bumping a provider CLI or Node pin.
- Adding or changing a toolchain Feature or the pip-extras Feature.
- Changing how a run selects Features, or how images are built from them.
- Changing the interpreter of in-container tool servers.

## Steps

1. **Put the Feature in its owner's project folder.** A project folder is an
   owner's `features/` directory with a source folder holding one folder per Feature
   (named by its id) and a test folder holding one test script per Feature, the layout
   `devcontainer features test` expects. The agent layer is owned by
   `vs-agent`, toolchains by the catalog in `vs-sandbox`, run environment
   extras by `vs-runtime`. A new project folder must be added to the
   `feature-tests` CI job, to both Docker cache keys in that workflow, and to
   the `agent_image` component's roots in the repoctl component config.
   `test_the_feature_tests_job_runs_the_project` fails if it is not.
2. **Keep the metadata inside the supported subset.** The generator honours
   only the fields it parses and rejects anything that needs the container
   runtime at start (mounts, privileges, lifecycle commands) by name. Options
   are strings or booleans, each with a default that install script reads as the
   upper-cased option id.
   `test_any_field_outside_the_supported_set_is_rejected` fails if not.
3. **Install everything the Feature uses.** Rely on another Feature only
   through `installsAfter`; `dependsOn` must stay empty because the caller
   lists every Feature. `test_the_project_is_well_formed` fails on a script
   that names an undeclared sibling. A missing `apt` package shows only in a
   real build: run `feature-tests`.
4. **Take every version as an option whose default equals the owner's pin.**
   The pins live with the agent policy, the in-container interpreter module
   and the toolchain catalog; the selecting code passes them explicitly.
   `test_option_defaults_mirror_provider_policy` fails if a default drifts.
5. **Register a toolchain in the catalog.** Add its name, pins and options to
   the toolchain catalog in `vs-sandbox`. The catalog's order is install
   order, least often changed first, because a change rebuilds every later
   layer.
   `test_every_toolchain_has_a_feature_folder_and_every_folder_a_toolchain`
   fails if a toolchain and its folder disagree.
6. **Route every network fetch through the helper block.** Copy the block
   between the `vibesys network helpers` markers verbatim from an existing
   install script and use its `fetch`, `apt_install`, `npm_install_global`,
   `pip_install` or `retry`; a bare `curl`, `apt-get`, `npm install`,
   `pip install` or `uv` fetch is rejected.
   `test_no_network_command_runs_outside_the_helpers`
7. **Put environment in the Feature's own `containerEnv`**, not a shared file.
   `test_container_env_follows_its_features_install_and_precedes_the_next_copy`
8. **Select the Feature through the image-features function in
   `vs_runtime.api.infrastructure`**, never by building a Feature list in a run
   environment. It deduplicates and sorts the selection, so its order never
   changes the image cache key.
   `test_order_and_repeats_of_the_selection_do_not_change_the_result`
9. **Run VibeSys's own processes on the agent image's Python.** Start a tool
   server in the container through the `vs-agent` containerizing helper,
   which sets the interpreter and import path. A server may import only the
   packages the helper declares as available there, and those only with what
   the agent Feature installs into that interpreter.
   `test_every_server_starts_in_the_container_on_the_one_interpreter_and_import_path`
10. **Build the real bases by hand before landing a Feature change.** CI tests
    small images of each base family; the backends' GPU bases are too large to
    pull there. Build the agent image on them and put the result in the PR.
    Unenforced: the bases cannot be pulled within a CI job's budget.

## Testing

- Unit tests drive the Feature image build and the agent image build through
  an injected Docker command runner and assert on the argv and the generated
  Dockerfile; they never call Docker.
  `test_agent_image_is_one_docker_build_of_the_generated_dockerfile`
- A real build that runs the installed tools is an e2e test, opt-in with
  `VIBESYS_E2E_DOCKER=1`, and runs in CI's `agent-image` job through the
  `agent_image_tests` group. Add a case there when a Feature installs
  something a run depends on.
  `test_cpu_agent_image_runs_every_shipped_cli_as_the_agent_user`
- Conformance runs the `devcontainer` CLI's feature tests on a base and the
  project folders; it mounts the Docker socket, which CI runners allow.
  `test_the_feature_tests_job_runs_the_project`

## Known failure modes

- BuildKit cannot use a bare `sha256:` image ID in `FROM`; the agent layer
  failed on task images until the build tagged an ID base first.
  `test_agent_base_image_is_never_a_bare_image_id`
- A base image that already has an account at the agent's uid (the ROCm
  PyTorch image's `jenkins`) kept its group name, so the confinement's
  `chown agent:agent` failed; the agent Feature renames both user and group.
  `test_agent_user_and_group_are_both_named_agent_over_a_uid_1000_base`
- A stalled download hung the image build until the run's budget ran out;
  every fetch is bounded and retried, and the whole build has one budget whose
  expiry stops Docker and names the running step.
  `test_an_agent_image_build_that_never_finishes_is_stopped_with_its_output`
- Docker output kept whole for error messages grew without bound; diagnostics
  keep a bounded tail.
  `test_truncated_failure_diagnostics_are_marked_and_bounded`
- Tool servers started on the base image's `python` (conda 3.10 on ROCm, none
  on plain Ubuntu) could not import the framework.
  `test_every_server_starts_in_the_container_on_the_one_interpreter_and_import_path`
- A build that mounts the Docker socket is refused by a hardened daemon's
  authorization plugin, so run-time builds mount nothing.
  `test_a_build_never_mounts_the_docker_socket_or_runs_a_container`
- The Go pin is also written into the remote evaluator setup command in
  `vs-runtime`, and that code's closed set of evaluator toolchains duplicates
  part of the toolchain catalog; change both together. Unenforced: no test
  ties them yet.
- Package data is declared one level deep, so a file in a subfolder of a
  Feature is missing from an installed wheel. Unenforced: only the agent
  Feature has a package-data test (`test_agent_feature_ships_as_package_data`).

## Sources of truth

- [devcontainer-features.md](../../../docs/contributing/devcontainer-features.md):
  design, Feature owners, supported metadata, CI bases.
- The Feature-to-Dockerfile generator and image builder in `vs-sandbox`, and
  its toolchain catalog (names and pins).
- The agent policy pins, the in-container interpreter module and the agent
  image builder with its budget, all in `vs-agent`.
- The `feature-tests` and `agent-image` CI jobs and the `agent_image_tests`
  check group.
