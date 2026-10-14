---
name: change-container-images
description: Change the image every run's agent container starts from, by adding or changing a Dev Container Feature (the agent layer, a toolchain such as Rust or Go, the container runtime, pip extras) or the mechanism that builds it. Use when editing a features/ folder under libs/vs-agent, libs/vs-sandbox or libs/vs-runtime, toolchain_features.py, feature_image.py, feature_dockerfile.py, vs_agent images.py or agent_feature.py, container_python.py, provider CLI or Node pins, or the feature-tests and agent-image CI jobs.
metadata:
  kind: codebase
---

# Change container images

Every run's agent runs in a local Docker container (see "run environment" in
[sandboxing.md](../../../docs/contributing/sandboxing.md)). Its image is the
task image, or a backend base image, plus layers built from
[Dev Container Features](https://containers.dev/implementors/features/): a
Feature is a folder with `devcontainer-feature.json` (id, options,
environment) and an `install.sh` that runs as root during the build. VibeSys
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

- Adding a CLI, system package or Python package to the agent layer
  (`libs/vs-agent/src/vs_agent/images/features/`), or bumping a pin in
  `libs/vs-agent/src/vs_agent/provider_policy.py`.
- Adding or changing a toolchain Feature (`libs/vs-sandbox/src/vs_sandbox/features/`,
  `libs/vs-sandbox/src/vs_sandbox/toolchain_features.py`) or the pip-extras
  Feature (`libs/vs-runtime/src/vs_runtime/features/`).
- Changing how a run selects Features (`libs/vs-runtime/src/vs_runtime/_image_features.py`)
  or how images are built (`libs/vs-sandbox/src/vs_sandbox/feature_image.py`,
  `libs/vs-sandbox/src/vs_sandbox/feature_dockerfile.py`,
  `libs/vs-agent/src/vs_agent/images.py`).
- Changing the interpreter of in-container tool servers (`libs/vs-agent/src/vs_agent/container_python.py`).

## Steps

1. **Put the Feature in its owner's project folder.** A project folder is an
   owner's `features/` directory with `src/` holding one folder per Feature
   (named by its id) and `test/` holding one `test.sh` per Feature, the layout
   `devcontainer features test` expects. The agent layer is owned by
   `vs_agent`, toolchains by the catalog in `vs_sandbox`, run environment
   extras by `vs_runtime`. A new project folder must be added to the
   `feature-tests` job, to both Docker cache keys in that workflow, and to the
   `agent_image` component's roots in `.repoctl/components.toml`.
   `tests/quality/test_feature_projects_wired.py`
2. **Keep the metadata inside the supported subset.** The generator honours
   only the fields `vs_sandbox.feature_dockerfile` parses and rejects anything
   that needs the container runtime at start (mounts, privileges, lifecycle
   commands) by name. Options are strings or booleans, each with a default
   that `install.sh` reads as the upper-cased option id.
   `libs/vs-sandbox/tests/test_feature_dockerfile.py:test_any_field_outside_the_supported_set_is_rejected`
3. **Install everything the Feature uses.** Rely on another Feature only
   through `installsAfter`; `dependsOn` must stay empty because the caller
   lists every Feature. `feature_project_problems` rejects a script that names
   an undeclared sibling
   (`tests/quality/test_feature_projects_wired.py:test_the_project_is_well_formed`).
   A missing `apt` package shows only in a real build: run `feature-tests`.
4. **Take every version as an option whose default equals the owner's pin.**
   Pins live in `vs_agent.provider_policy`, `vs_agent.container_python` and
   `vs_sandbox.toolchain_features`; the selecting code passes them explicitly.
   `libs/vs-agent/tests/test_agent_feature.py:test_option_defaults_mirror_provider_policy`
5. **Register a toolchain in the catalog.** Add its name, pins and options to
   `vs_sandbox.toolchain_features` (`KNOWN_TOOLCHAINS` order is install order,
   least often changed first, because a change rebuilds every later layer).
   `libs/vs-sandbox/tests/test_toolchain_features.py:test_every_toolchain_has_a_feature_folder_and_every_folder_a_toolchain`
6. **Route every network fetch through the helper block.** Copy the block
   between the `vibesys network helpers` markers verbatim from an existing
   `install.sh` and use `fetch`, `apt_install`, `npm_install_global`,
   `pip_install` or `retry`; a bare `curl`, `apt-get`, `npm install`,
   `pip install` or `uv` fetch fails the check.
   `tests/quality/test_feature_network_steps.py`
7. **Put environment in the Feature's own `containerEnv`**, not a shared file.
   `libs/vs-sandbox/tests/test_feature_dockerfile.py:test_container_env_follows_its_features_install_and_precedes_the_next_copy`
8. **Select the Feature through `vs_runtime.api.infrastructure.image_features`**,
   never by building a Feature list in a run environment. It deduplicates and sorts the
   selection, so its order never changes the image cache key.
   `libs/vs-runtime/tests/test_image_features.py`
9. **Run VibeSys's own processes on the agent image's Python.** Start a tool
   server in the container through `containerize_server` in
   `vs_agent.container_python`, which sets the interpreter and import path.
   It may import only the packages in `CONTAINER_SERVER_PACKAGES`, and those
   only with what the agent Feature installs into that interpreter.
   `tests/vibesys/agents/test_container_servers.py`
10. **Build the real bases by hand before landing a Feature change.** CI tests
    small images of each base family; the backends' GPU bases are too large to
    pull there. Build with `agent_image` on them and put the result in the PR.
    Unenforced: the bases cannot be pulled within a CI job's budget.

## Testing

- Unit tests drive `build_feature_image` and `agent_image` through an
  injected Docker command runner and assert on the argv and the generated
  Dockerfile; they never call Docker. `libs/vs-agent/tests/test_images.py`
- A real build that runs the installed tools is an e2e test, opt-in with
  `VIBESYS_E2E_DOCKER=1`, and runs in CI's `agent-image` job through the
  `agent_image_tests` group. Add a case there when a Feature installs
  something a run depends on. `tests/e2e/test_agent_image_e2e.py`
- Conformance runs `libs/vs-sandbox/src/vs_sandbox/devcontainer-cli/test_features.sh`
  on a base and project folders; it mounts the Docker socket, which CI runners
  allow. `.github/workflows/test.yml`

## Known failure modes

- BuildKit cannot use a bare `sha256:` image ID in `FROM`; the agent layer
  failed on task images until `agent_image` tagged an ID base first (#1522).
  `libs/vs-agent/tests/test_images.py:test_agent_base_image_is_never_a_bare_image_id`
- A base image that already has an account at the agent's uid (the ROCm
  PyTorch image's `jenkins`) kept its group name, so the confinement's
  `chown agent:agent` failed; the agent Feature renames both user and group (#1589).
  `tests/e2e/test_agent_image_e2e.py:test_agent_user_and_group_are_both_named_agent_over_a_uid_1000_base`
- A stalled download hung the image build until the run's budget ran out
  (#2037); every fetch is bounded and retried, and the whole build has one
  budget whose expiry stops Docker and names the running step.
  `libs/vs-agent/tests/test_docker_build_runner.py:test_an_agent_image_build_that_never_finishes_is_stopped_with_its_output`
- Docker output kept whole for error messages grew without bound (#1925);
  diagnostics keep a bounded tail.
  `libs/vs-agent/tests/test_docker_build_runner.py:test_truncated_failure_diagnostics_are_marked_and_bounded`
- Tool servers started on the base image's `python` (conda 3.10 on ROCm, none
  on plain Ubuntu) could not import the framework (#1653).
  `tests/vibesys/agents/test_container_servers.py`
- A build that mounts the Docker socket is refused by a hardened daemon's
  authorization plugin, so run-time builds mount nothing (#1898).
  `libs/vs-sandbox/tests/test_feature_image.py:test_a_build_never_mounts_the_docker_socket_or_runs_a_container`
- The Go pin is also written into the remote evaluator setup command in
  `libs/vs-runtime/src/vs_runtime/_trusted_evaluation_preparation.py`, and that
  module's `EvaluatorToolchain` closed set duplicates part of
  `KNOWN_TOOLCHAINS`; change both together. Unenforced: no test ties them yet.
- Package data is declared one level deep (`features/src/*/*`), so a file in a
  subfolder of a Feature is missing from an installed wheel. Unenforced: only
  the agent Feature has a package-data test
  (`libs/vs-agent/tests/test_images.py:test_agent_feature_ships_as_package_data`).

## Sources of truth

- [devcontainer-features.md](../../../docs/contributing/devcontainer-features.md):
  design, Feature owners, supported metadata, CI bases.
- `vs_sandbox.feature_dockerfile`, `vs_sandbox.feature_image`: generator and
  build; `vs_sandbox.toolchain_features`: toolchain names and pins.
- `vs_agent.provider_policy` and `vs_agent.container_python`: agent-layer pins
  and the in-container interpreter; `vs_agent.images`: `agent_image` and its budget.
- `.github/workflows/test.yml` (`feature-tests`, `agent-image`) and
  `.repoctl/checks.toml` (`agent_image_tests`).
