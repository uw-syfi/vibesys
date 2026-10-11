# Composing run images from Dev Container Features

This page records the design and the phase 1 spike for roadmap
[#1837](https://github.com/uw-syfi/vibesys/issues/1837) (spike issue
[#1842](https://github.com/uw-syfi/vibesys/issues/1842)). Nothing here changes
how a run builds its image: `agent_image` still builds
`libs/vs-agent/src/vs_agent/images/agent.Dockerfile`.

## Design

A [Dev Container Feature](https://containers.dev/implementors/features/) is a
directory with a `devcontainer-feature.json` (id, options, environment) and an
`install.sh` that runs as root during the image build. The reference
`devcontainer` CLI reads a `devcontainer.json` (a base `image` plus a
`features` map), builds one image layer per Feature in order, and caches each
layer separately.

VibeSys's layers become repo-local Features under
`libs/vs-agent/src/vs_agent/images/features/`:

| Feature | Owns |
| --- | --- |
| `vibesys-agent` | Node, the four provider CLIs, uv, VibeSys's Python with `mcp`, the non-root `agent` user (uid 1000), `/workspace` |
| `vibesys-rust` | rustup and the pinned Rust toolchain in `/opt/rustup` and `/opt/cargo`, plus `PATH` for plain and login shells |

Rules every Feature follows:

- It installs every package it uses. It relies on another Feature only through
  `installsAfter` or `dependsOn`. `libs/vs-agent/tests/test_devcontainer_features.py`
  checks that the metadata parses, that declared ids exist, and that an
  `install.sh` never names another Feature it did not declare.
- It takes versions as options. The option defaults equal the pins in
  `vs_agent.provider_policy`, and the test above fails if they drift, so the
  pins have one source. The generated `devcontainer.json` passes them
  explicitly anyway.
- Its environment (`PATH`, `RUSTUP_HOME`) is `containerEnv` in its own
  metadata, not a line in a shared file.

The layout under `features/` is the one `devcontainer features test` expects:
`src/<id>/` holds the Feature and `test/<id>/test.sh` holds its checks. The
checks run against whatever base is passed with `-i`, so one command covers
each base family.

A generated `devcontainer.json` for a run looks like this (the spike generator
filled `image` and the options from `provider_policy`):

```json
{
  "image": "python:3.12-bookworm",
  "features": {
    "./features/vibesys-agent": {"node_version": "24.21.0", "claude_version": "2.1.268"},
    "./features/vibesys-rust": {"rust_version": "1.92.0"}
  },
  "containerUser": "agent"
}
```

## Choosing how to invoke the devcontainer CLI

The CLI (`@devcontainers/cli`, a Node.js program) was run both ways, pinned to
0.89.0, on both bases, and produced images that pass the same checks.

| | Host install: `npx @devcontainers/cli@0.89.0` | CLI in a container, Docker socket mounted |
| --- | --- | --- |
| Host requirement | Node.js 20 or newer, plus network to npm on first use | Docker only, which VibeSys already requires |
| Pinning | Version in the command; Node itself is whatever the host has (24.12 here) | Whole toolchain pinned by an image built from `features/devcontainer-cli.Dockerfile` (Node, docker CLI, buildx, devcontainer CLI) |
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

## Gaps found

These are things the Dockerfile does that a Feature cannot express, and phase 2
must handle them.

- **`USER agent` and `WORKDIR /workspace`.** The CLI-built image keeps the
  base's user (root) and working directory (`/var/lib/jenkins` on ROCm). The
  user only appears in the `devcontainer.metadata` label as `containerUser`.
  `agent_image` needs either a one-line trailing `docker build` that sets
  both, or `docker run --user agent --workdir /workspace` at run start.
- **Selecting Features per run.** Today `TOOLCHAINS` is a build arg. With
  Features it is the set of keys in the generated `devcontainer.json`; `go`
  and `container-runtime` still need Features.
- **`ENV HOME`** is not needed: Docker derives `HOME` from the user.

## Status

| Part | State |
| --- | --- |
| `vibesys-agent` and `vibesys-rust` Features, metadata test | Built (this spike) |
| Image builds on Debian and ROCm through the CLI, both invocations | Built and measured (this spike) |
| `devcontainer features test` on Debian and ROCm | Run by hand (this spike) |
| `devcontainer features test` in CI on Debian, ROCm, and CUDA | Not built (phase 2) |
| `go` and `container-runtime` Features, Modal extra (`pip_extras`) | Not built (phase 2); `pip_extras` option exists, untested |
| `agent_image` building through the CLI, trailing `USER` and `WORKDIR` | Not built (phase 2) |
| Evaluator tools as Features | Not built (phase 3) |
| Task `devcontainer.json`, deleting `agent.Dockerfile` | Not built (phase 4) |
