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
`install.sh` that runs as root during the image build. The reference
`devcontainer` CLI reads a `devcontainer.json` (a base `image` plus a
`features` map), builds one image layer per Feature in order, and caches each
layer separately.

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
| Image mechanism: `devcontainer.json` from a base plus Feature references (local folder or OCI reference), containerized CLI, content-addressed tag, final user and working directory, `feature_project_problems`, the per-base test runner | `vs_sandbox.feature_image` | Nothing about agents, Modal, or evaluator tools |
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
  if they drift, so each pin has one source. The generated `devcontainer.json`
  passes them explicitly anyway.
- Its environment (`PATH`, `RUSTUP_HOME`) is `containerEnv` in its own
  metadata, not a line in a shared file.

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
the list to `vs_sandbox.build_feature_image`, which generates, for `rust` on the
CPU base:

```json
{
  "image": "python:3.12-bookworm",
  "features": {
    "./features/vibesys-agent": {"node_version": "24.21.0", "claude_version": "2.1.268"},
    "./features/vibesys-rust": {"rust_version": "1.92.0"}
  },
  "overrideFeatureInstallOrder": ["./features/vibesys-agent", "./features/vibesys-rust"],
  "containerUser": "agent"
}
```

(The real file lists every option.) Without `overrideFeatureInstallOrder` the CLI
orders Features alphabetically, which would put the toolchains that change most
often ahead of the agent layer; the mechanism keeps the caller's order, so the
agent layer comes first.

`build_feature_image` then:

1. Builds the devcontainer CLI image from `vs_sandbox/devcontainer-cli/Dockerfile`
   (tag keyed by the file's hash, so an unchanged one is a layer-cache hit of
   about a second).
2. Writes the `devcontainer.json`, the local Feature folders, and a three-line
   finishing Dockerfile into a temporary workspace, and runs
   `docker run --rm -v /var/run/docker.sock:... -v <ws>:<ws> <cli image> build`.
   **The workspace is mounted at the same absolute path** inside the CLI
   container: the CLI passes that path to the host's Docker daemon as the build
   context, so a different path inside the container would name a directory the
   daemon cannot see.
3. Builds the finishing stage `FROM <features image>` with `WORKDIR` and `USER`
   set (`agent` and `/workspace` for `agent_image`), because the CLI keeps the
   base's `USER` and `WORKDIR` (`containerUser` only reaches the
   `devcontainer.metadata` label). It tags the result with a unique local tag and
   returns its immutable image ID, as the Dockerfile build did, then drops the
   intermediate tag.

The image ID is content-addressed: rebuilding the same selection on the same
base resolves to the same ID from the layer cache, even though the temporary
workspace has a different path each time. `push_agent_image` keys its GHCR tag
off that ID as before.

Features never depend on install order for correctness: a Feature that needs a
user another Feature creates (the container-runtime Feature's `users` option)
fails the install with a message if the user is missing.

## Choosing how to invoke the devcontainer CLI

The CLI (`@devcontainers/cli`, a Node.js program) was run both ways, pinned to
0.89.0, on both bases, and produced images that pass the same checks.

| | Host install: `npx @devcontainers/cli@0.89.0` | CLI in a container, Docker socket mounted |
| --- | --- | --- |
| Host requirement | Node.js 20 or newer, plus network to npm on first use | Docker only, which VibeSys already requires |
| Pinning | Version in the command; Node itself is whatever the host has (24.12 here) | Whole toolchain pinned by an image built from `vs_sandbox/devcontainer-cli/Dockerfile` (Node, docker CLI, buildx, devcontainer CLI) |
| One-time cost | First `npx` run downloads the package (3 s on a warm npm cache) | Image build 23 s, 449 MB on disk |
| Per-build overhead | None measured | Under 1 s (warm build 1.5 s against 3.3 s for the host CLI, within noise) |
| Pitfalls | Different host Node versions behave differently | Workspace must be mounted at the same absolute path; the container holds the Docker socket, which is root-equivalent on the host (the same trust Docker access already grants) |

**Decision: run the CLI in a container with the Docker socket mounted.** It
adds no Node.js requirement to the host, which the issue asks us to record as
a decision before adding, and it pins the Node version together with the CLI.
The cost is a 449 MB tool image and the same-path mount rule. If a deployment
cannot expose the socket to a container, the host `npx` form is the fallback;
both are the same `devcontainer build` arguments.

## Measured costs

Wall-clock seconds on one machine, one run each, so differences under about
20 percent are noise (network speed varies between runs). "Today" is
`agent_image(base, toolchains=("rust",))`'s Dockerfile with identical build
args. "Cold" disables the layer cache. "Warm" repeats an identical build.
"Change rust" edits only the Rust step or Feature. "Change agent" edits the
npm CLI step or the `vibesys-agent` Feature, which is the earliest step.

| Base | Build | Today | Features, host CLI | Features, CLI in container |
| --- | --- | --- | --- | --- |
| Debian (`python:3.12-bookworm`) | cold | 75 | 117 | 82 |
| | warm | 0.4 | 3.3 | 1.5 |
| | change rust | 24.5 | 24.8 | not run |
| | change agent | 87.1 | 96.6 | not run |
| ROCm (`rocm/pytorch:rocm6.3_ubuntu22.04_py3.10_pytorch_release_2.4.0`) | cold | 231 | 241 | 221 |
| | warm | 0.5 | 3.2 | not run |
| | change rust | 137 | 28.8 | not run |
| | change agent | 181 | 214 | not run |

`devcontainer features test` on both Features: 83 s on Debian, 237 s on ROCm,
all checks passed.

Reading the numbers:

- A warm rebuild is a few seconds either way. The CLI adds about 3 s for
  resolving Features and starting BuildKit.
- Cold builds cost about the same. Where the Feature build is slower it is
  the extra layers (one per Feature, plus the CLI's own bookkeeping layers).
- Changing one Feature rebuilds that Feature and every Feature after it.
  `vibesys-rust` is last, so a Rust change costs 29 s on ROCm, while in the
  Dockerfile the later Go, user, and `/workspace` steps rebuild too (137 s).
  This is an ordering effect, not a CLI effect: put the Features that change
  most often last.

## Gaps the spike found, and how phase 2 closed them

- **`USER agent` and `WORKDIR /workspace`.** The CLI-built image keeps the
  base's user (root) and working directory (`/var/lib/jenkins` on ROCm). The
  user only appears in the `devcontainer.metadata` label as `containerUser`.
  `agent_image` runs a three-line finishing build (`WORKDIR /workspace`,
  `USER agent`) on the CLI's image. The alternative, `docker run --user agent
  --workdir /workspace` at run start, would put the fix in every one of the
  four run environments and in `docker exec` paths; one build step is the one
  owner.
- **Selecting Features per run.** `TOOLCHAINS` and `PIP_EXTRAS` are no longer
  build args: they are the keys of the generated `devcontainer.json`.
- **`ENV HOME`** is not needed: Docker derives `HOME` from the user.
- **Feature order.** The CLI orders Features alphabetically unless
  `overrideFeatureInstallOrder` is set; the generator sets it.
- **Group membership across Features.** The Dockerfile added `agent` to the
  `docker` group in a later step. The container-runtime Feature takes a `users`
  option, creates the group, and adds those users, failing loudly if one does
  not exist yet.

## Testing Features in CI

`vs_sandbox/devcontainer-cli/test_features.sh <base> <project>...` runs
`devcontainer features test` for every Feature in each project folder (one per
owner) on one base, with the same containerized CLI `agent_image` uses. The
`feature-tests` job in `.github/workflows/test.yml` runs it for one base per
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
the measured results are in the PR that last changed one.

When a Feature test is run through the containerized CLI, the CLI stages test
projects under `/tmp/devcontainercli` and gives the host daemon that path as a
bind-mount source, so the script mounts that directory at the same path.

## Status

| Part | State |
| --- | --- |
| Five Features, each with a `test.sh`, in their owners' folders; `feature_project_problems` checks every project | Built |
| Image mechanism in `vs_sandbox` (generation, containerized CLI, final user and workdir, content-addressed tag) | Built |
| Toolchain catalog in `vs_sandbox` with the Rust, Go, and container-runtime pins | Built |
| `image_features` selection in `vs_runtime` (pure, property-tested) | Built |
| `agent_image` as a thin wrapper that passes the agent Feature first | Built |
| `devcontainer features test` in CI on Debian, ROCm Ubuntu, and CUDA Ubuntu | Built (`feature-tests` job) |
| `agent.Dockerfile` | Deleted |
| Evaluator tools as Features, each beside its tool | Not built (phase 3) |
| Task `devcontainer.json`; toolchains declared by tasks instead of the catalog | Not built (phase 4) |
