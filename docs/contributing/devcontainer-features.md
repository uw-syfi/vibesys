# Composing run images from Dev Container Features

This page records the design for roadmap
[#1837](https://github.com/uw-syfi/vibesys/issues/1837): the phase 1 spike
([#1842](https://github.com/uw-syfi/vibesys/issues/1842)) and phase 2
([#1873](https://github.com/uw-syfi/vibesys/issues/1873)), where
`vs_agent.api.images.agent_image` builds every run's editor image from Features
and `agent.Dockerfile` is gone.

## Design

A [Dev Container Feature](https://containers.dev/implementors/features/) is a
directory with a `devcontainer-feature.json` (id, options, environment) and an
`install.sh` that runs as root during the image build. The specification says
what building with Features means: for each Feature in order, make its folder
available, export its options as environment variables (the option id,
upper-cased), run `install.sh` as root, and apply the Feature's `containerEnv`.
The reference `devcontainer` CLI does that by driving Docker, one image layer per
Feature.

**At run time VibeSys does not use the CLI.** `vs_sandbox.feature_dockerfile`
writes those same steps as a Dockerfile and `docker build` runs it. The CLI is
the authoring and CI tool: `devcontainer features test` keeps proving that each
Feature is specification-conformant, which is what the generator relies on.

VibeSys's layers are repo-local Features. **Each Feature lives with the module
that owns what it installs, and is passed to the image mechanism as data.** The
mechanism never imports an owner; owners import the mechanism.

| Feature | Owner | Folder | Installs |
| --- | --- | --- | --- |
| `vibesys-agent` | `vs_agent` | `vs_agent/images/features/` | Node, the four provider CLIs, uv, VibeSys's Python with `mcp`, the non-root `agent` user (uid 1000), `/workspace`; versions from `provider_policy` |
| `vibesys-rust` | `vs_sandbox` toolchain catalog | `vs_sandbox/features/` | rustup and the pinned Rust toolchain in `/opt/rustup` and `/opt/cargo`, plus `PATH` for plain and login shells |
| `vibesys-go` | `vs_sandbox` toolchain catalog | `vs_sandbox/features/` | The pinned Go toolchain in `/usr/local/go` |
| `vibesys-container-runtime` | `vs_sandbox` toolchain catalog | `vs_sandbox/features/` | A static Docker engine, the compose plugin, kind, kubectl, and the `docker` group, for container-topology tasks |
| `vibesys-pip-extras` | `vs_runtime`, beside the run environments | `vs_runtime/features/` | Extra pip requirements a run environment needs in the editor container (the Modal client) |

The toolchain catalog lives in `vs_sandbox` (and owns the Rust, Go, and
container-runtime version pins) until tasks declare their own toolchains in
phase 4. Evaluator tools are not Features yet (phase 3); each will live beside
its tool.

Ownership of the moving parts:

| Part | Module | Knows about |
| --- | --- | --- |
| Dockerfile generation: Feature metadata and options in, Dockerfile and build-context file list out (pure), with the supported metadata fields | `vs_sandbox.feature_dockerfile` | Nothing about agents, Modal, or evaluator tools |
| Image mechanism: reads Feature folders, writes the build context, runs `docker build`, content-addressed tag, `feature_project_problems`, the per-base test runner | `vs_sandbox.feature_image` | Nothing about agents, Modal, or evaluator tools |
| Toolchain catalog: names, pins, Feature options | `vs_sandbox.toolchain_features` | Feature folders it owns |
| `agent_image`: resolves the base (task image, bare ID), puts the agent Feature first, passes the caller's Features | `vs_agent.images` | The agent Feature and `provider_policy`; not what the extras are |
| Selecting a run's Features (`image_features`: pure, property-tested) | `vs_runtime` | Which toolchains a task needs, which pip extras an environment adds |

Dependencies point downward only: `vs_runtime` imports `vs_agent` and
`vs_sandbox`; `vs_agent` imports `vs_sandbox`; `vs_sandbox` imports neither. No
`tach.toml` edge was added or removed.

Rules every Feature follows:

- It installs every package it uses. It relies on another Feature only through
  `installsAfter` or `dependsOn`. `vs_sandbox.api.feature_project_problems`, run
  by each owner's tests over its own folder, checks that the metadata parses,
  that every option has a default and is read by the script, that every Feature
  has a `test.sh`, and that an `install.sh` never names a sibling Feature it
  did not declare.
- It takes versions as options. The option defaults equal the pins in the
  owner's policy (`vs_agent.provider_policy` for the agent Feature,
  `vs_sandbox.toolchain_features` for the catalog), and the owner's test fails
  if they drift, so each pin has one source. The generator passes them
  explicitly anyway.
- Its environment (`PATH`, `RUSTUP_HOME`) is `containerEnv` in its own
  metadata, not a line in a shared file.
- Every network step is bounded and retried at most five times. The script
  carries a helper block (`retry`, `fetch`, `apt_install`, `npm_install_global`,
  `pip_install`) that sets connect, stall, and request timeouts; Features cannot
  share files, so the block is duplicated and
  `tests/quality/test_feature_network_steps.py` keeps the copies identical and
  rejects a bare `curl`, `apt-get`, `npm install`, `pip install`, or `uv` fetch
  outside them. The whole image build also has one budget
  (`agent_image(timeout=...)`, `DockerEnvironmentConfig.image_build_timeout_seconds`);
  when it runs out, Docker is stopped and `ImageBuildTimeoutError` names the
  running Dockerfile step and carries the tail of the build output.

The layout of each owner's `features/` folder is the one `devcontainer features test` expects:
`src/<id>/` holds the Feature and `test/<id>/test.sh` holds its checks. The
checks run against whatever base is passed with `-i`, so one command covers
each base family.

## How `agent_image` uses them

`vs_runtime.image_features(toolchains, pip_extras)` is a pure function from a
run environment's declaration to Feature references, in install order: the
pip-extras Feature when there are requirements, then the toolchains in catalog
order. Selections are deduplicated and sorted, an unknown toolchain name or a
requirement containing whitespace is an error, and the agent user joins the
`docker` group through the container-runtime Feature's `users` option.
`vs_agent.agent_image(base, features=...)` puts the agent Feature first and hands
the list to `vs_sandbox.build_feature_image`. For `rust` on the CPU base the
generated Dockerfile is (abridged to the first two Features and the last lines):

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
# Feature vibesys-agent
COPY features/vibesys-agent /usr/src/vibesys-features/vibesys-agent
RUN cd /usr/src/vibesys-features/vibesys-agent \
 && chmod +x ./install.sh \
 && set -a && . ./devcontainer-features.env && set +a \
 && ./install.sh \
 && rm -rf /usr/src/vibesys-features/vibesys-agent
ENV PIP_BREAK_SYSTEM_PACKAGES="1"
# Feature vibesys-rust
COPY features/vibesys-rust /usr/src/vibesys-features/vibesys-rust
RUN ...
ENV RUSTUP_HOME="/opt/rustup"
ENV CARGO_HOME="/opt/cargo"
ENV PATH="/opt/cargo/bin:${PATH}"
WORKDIR /workspace
USER agent
```

`devcontainer-features.env` is generated next to each Feature's `install.sh`.
It holds the Feature's options (the caller's value, or the metadata default)
as shell-quoted `NAME=value` lines, plus `_REMOTE_USER` and `_CONTAINER_USER`
(`root`) and their home directories, the variables the CLI also provides. Each
Feature is copied just before its own install step, so changing one Feature
keeps every layer before it cached. The caller's order is the install order;
the CLI's alphabetical default (which would put the toolchains that change most
often ahead of the agent layer) does not apply.

`build_feature_image` then:

1. Reads each Feature's `devcontainer-feature.json` and rejects what it cannot
   honour (below).
2. Writes the Feature folders, their options files, and the Dockerfile into a
   temporary directory and runs
   `docker build --build-arg BASE_IMAGE=<base> --tag <unique local tag> ...`.
   No container is started and no host path is mounted. The base is a build
   argument, so the Dockerfile text does not depend on it.
3. Resolves the tag to its immutable image ID, as the old Dockerfile build did.

The tag is derived from the build inputs, so when the daemon already has it
steps 2 and 3 reduce to the inspection: nothing is built or exported. A base
named by a mutable registry tag is an input known by name only, so callers pin
it.

The image ID is content-addressed: rebuilding the same selection on the same
base resolves to the same ID from the layer cache, even though the temporary
build directory has a different path each time. `push_agent_image` keys its
GHCR tag off that ID as before.

**Supported Feature metadata.** `id`, `version`, `name`, `description`,
`documentationURL`, `options` (type `string` or `boolean`, each with a
default), `containerEnv`, `installsAfter` (checked against the given order) and
`dependsOn` (only empty, because VibeSys lists every Feature a build needs).
Any other field (`mounts`, `privileged`, `init`, `capAdd`, `securityOpt`,
`entrypoint`, `customizations`, lifecycle commands) would need the container
runtime to act on it, which an image build cannot do, so it is rejected with an
error naming the field instead of being ignored. The authoring check
`feature_project_problems` runs the same parser. OCI-published Features are not
supported either: nothing in the build fetches them, so there is no type for one.

Features never depend on install order for correctness: a Feature that needs a
user another Feature creates (the container-runtime Feature's `users` option)
fails the install with a message if the user is missing.

## Choosing how to build the image

Three ways were built and run on both bases; all produced images that pass the
same checks. The first design (phase 2 as first reviewed) used the second row;
a real cluster host forced the change.

| | Containerized CLI, Docker socket mounted | CLI installed on the host | Generated Dockerfile, plain `docker build` (chosen) |
| --- | --- | --- | --- |
| Host requirement | Docker, and a daemon that allows `-v /var/run/docker.sock` | Docker plus Node.js 20 or newer, plus network to npm on first use | Docker only |
| Works where the daemon forbids the socket mount | No: `authorization denied by plugin authz-guard: bind-mount of sensitive host path is not allowed: /var/run/docker.sock` (exit 125) | Yes | Yes |
| Pinning | Whole toolchain pinned by an image built from `vs_sandbox/devcontainer-cli/Dockerfile` (Node, docker CLI, buildx, devcontainer CLI) | CLI version in the command; Node is whatever the host has | The generator is VibeSys code; the Docker CLI is the host's |
| Extra moving parts at run time | A 449 MB tool image, the same-absolute-path mount rule, a root-equivalent socket inside a container | A Node install on every host | None |
| Spec conformance | The reference implementation | The reference implementation | Implements the specification's install steps for the supported fields; `devcontainer features test` in CI proves the Features themselves |

**Decision: generate a Dockerfile.** A hardened site is exactly where VibeSys
should still work, a Node requirement on every host is a cost the issue asked
us to avoid, and the Dockerfile needs nothing a Feature does not already
declare. The cost is that VibeSys now owns the small part of the specification
the CLI implemented (about 300 lines, pure, property-tested) and refuses the
fields it does not implement. The CLI stays the conformance oracle in CI.
Both sides were compared on the same bases (below): identical runtime
configuration, tool versions, installed packages, and `/usr/local/bin` and
`/opt` contents.

## Measured costs

Wall-clock seconds on one machine, one run each, so differences under about
20 percent are noise (network speed varies between runs). Both builds select
`rust` and `go` through `agent_image` and start cold: the base is the named
image plus one extra layer that is unique per build, so no layer is cached.
"Warm" repeats the identical build.

| Base | Build | Containerized CLI (socket mounted) | Generated Dockerfile |
| --- | --- | --- | --- |
| Debian (`python:3.12-bookworm`) | cold | 98.1 | 105.8 |
| | warm | not measured | 0.4, same image ID |
| ROCm (`rocm/pytorch:rocm6.3_ubuntu22.04_py3.10_pytorch_release_2.4.0`) | cold | 279.0 | 221.3 |
| | warm | not measured | 0.3, same image ID |

Earlier measurements of the CLI path, before this change (host CLI 3.3 s warm,
containerized CLI 1.5 s warm) and of the hand-maintained Dockerfile it replaced
(75 s cold on Debian, 231 s on ROCm) put all of these within noise of each
other for cold builds. A warm rebuild now costs under a second because there is
no CLI to start.

Equivalence checks, run on both images of each base (the same script,
`diff` of the reports empty):

- Image config: user `agent`, working directory `/workspace`, `PATH`,
  `RUSTUP_HOME`, `CARGO_HOME`, `PIP_BREAK_SYSTEM_PACKAGES`.
- Run with no flags: uid 1000 and `pwd` of `/workspace`; `claude` 2.1.304,
  `codex` 0.160.0, `gemini` 0.59.0, `opencode` 1.18.30; Node 24.21.0; VibeSys's
  Python imports `mcp`; `cargo` and `rustc` 1.92.0; Go 1.23.12; `uv`; `cargo` and
  `go` found by a login shell.
- The sorted list of installed Debian packages and the contents of
  `/usr/local/bin` and `/opt` hash to the same value.

Changing one Feature rebuilds that Feature and every Feature after it, so the
Features that change most often go last (`vibesys-rust` and `vibesys-go` follow
the agent layer).

## Gaps the spike found, and how phase 2 closed them

- **`USER agent` and `WORKDIR /workspace`.** The CLI keeps the base's user
  (root) and working directory (`/var/lib/jenkins` on ROCm); the user only
  appears in the `devcontainer.metadata` label. The generated Dockerfile ends
  with `WORKDIR /workspace` and `USER agent`, so the image runs as `agent` with
  no flags. The alternative, `docker run --user agent --workdir /workspace` at
  run start, would put the fix in every one of the four run environments and in
  `docker exec` paths; the Dockerfile is the one owner. The image carries no
  `devcontainer.metadata` label, which nothing in VibeSys reads.
- **Selecting Features per run.** `TOOLCHAINS` and `PIP_EXTRAS` are no longer
  build args: they select which Features the Dockerfile copies and installs.
- **`ENV HOME`** is not needed: Docker derives `HOME` from the user.
- **Feature order.** The generator installs in the caller's order.
- **Group membership across Features.** The Dockerfile added `agent` to the
  `docker` group in a later step. The container-runtime Feature takes a `users`
  option, creates the group, and adds those users, failing loudly if one does
  not exist yet.
- **A hardened Docker daemon** (#1898 follow-up). The containerized CLI needed
  `-v /var/run/docker.sock`, which a daemon with an authorization plugin
  refuses. The generated Dockerfile mounts nothing; a test asserts no build
  argv names the socket, a volume, or a container run.

## Testing Features in CI

`vs_sandbox/devcontainer-cli/test_features.sh <base> <project>...` runs
`devcontainer features test` for every Feature in each project folder (one per
owner) on one base, with the pinned containerized CLI. This is the one place
the CLI and the socket mount remain: CI runners allow it, a run never uses it.
The `feature-tests` job in `.github/workflows/test.yml` runs it for one base per
family, with Docker's storage cached between runs (the mechanism `agent-image`
uses):

| Family | CI base | Why this base |
| --- | --- | --- |
| Debian | `python:3.12-bookworm` | The CPU backend's default image |
| ROCm Ubuntu | `rocm/dev-ubuntu-22.04:6.3` (1.0 GB compressed) | Same distribution and ROCm user space as the backend's `rocm/pytorch` base |
| CUDA Ubuntu | `nvidia/cuda:12.4.1-base-ubuntu22.04` | Same distribution as the backend's `nvcr.io/nvidia/pytorch` base |

The backends' real default bases (`rocm/pytorch:...` at 23 GB compressed and
`nvcr.io/nvidia/pytorch:25.04-py3` at 25 GB) cannot be pulled inside a
10-minute job, so CI uses the small image of the same family. The real bases
are built by hand with `agent_image` before a change to a Feature lands, and
the measured results are in the PR that last changed one. Because runs build
with the generated Dockerfile and CI tests with the CLI, a change to a Feature
should also be built both ways on one base and the smoke checks compared (see
"Measured costs").

When a Feature test is run through the containerized CLI, the CLI stages test
projects under `/tmp/devcontainercli` and gives the host daemon that path as a
bind-mount source, so the script mounts that directory at the same path.

## Status

| Part | State |
| --- | --- |
| Five Features, each with a `test.sh`, in their owners' folders; `feature_project_problems` checks every project | Built |
| Dockerfile generator in `vs_sandbox` (pure, property-tested) and the image mechanism that runs `docker build` with it (final user and workdir, content-addressed tag) | Built |
| Run-time builds without a Docker socket mount | Built (tested through the injectable runner) |
| Toolchain catalog in `vs_sandbox` with the Rust, Go, and container-runtime pins | Built |
| `image_features` selection in `vs_runtime` (pure, property-tested) | Built |
| `agent_image` as a thin wrapper that passes the agent Feature first | Built |
| `devcontainer features test` in CI on Debian, ROCm Ubuntu, and CUDA Ubuntu | Built (`feature-tests` job) |
| `agent.Dockerfile` | Deleted |
| Evaluator tools as Features, each beside its tool | Not built (phase 3) |
| Task `devcontainer.json`; toolchains declared by tasks instead of the catalog | Not built (phase 4) |
