# Evaluator packages

This directory contains local development builds of reusable VibeSys evaluator
packages. Task definitions depend on package names and exact versions, not on
these source paths. The runtime resolves a package to immutable contents and
records its `sha256:` digest with the run.

Each immediate child is a self-contained package with a
`vibesys.evaluator.toml` file:

```toml
schema_version = 1
name = "vibesys-evaluator-example"
version = "0.1.0"
protocol_version = 1

[entrypoints]
example-check = ["example-check"]
```

Entry points are logical public names mapped to argv prefixes. Local source
packages may use the literal `${PACKAGE_ROOT}` token in an argv element. The
resolver expands it to the absolute package directory, allowing commands to run
from a candidate repository. Arguments declared by a task are appended to the
resolved prefix without invoking a shell. A task argument may use
`${PROJECT_ROOT}` when the evaluator needs an absolute path to the candidate.
The selected run environment expands it to the candidate repository root before
running the resolved command. This is necessary for tools such as `go -C`,
which change cwd.

## Candidate outputs

A package whose evaluator loads a file the candidate builds names it:

```toml
candidate_outputs = ["queue-candidate.so"]
```

Each entry is a normalized workspace-relative file path outside `.git/` and
`.vibesys/`. A task using the package must declare a `[build]` that produces
every entry; `vibesys preflight` and the example registry's `candidate-build`
check refuse one that does not. After the confined build, VibeSys verifies each
output (a regular file, not a symlink), records it outside the workspace, places
it at its path in the checkout where each trusted command runs, and passes the
paths, `:`-joined, in `VIBESYS_CANDIDATE_OUTPUTS`.

The local collection is the initial package source. Published packages can
later provide the same metadata and entry-point names through a registry-backed
resolver. Repository-specific checks and workloads belong with their task, not
in this directory.

## Go modules

A source package whose entrypoints use `go run` declares its package-relative
module root explicitly:

```toml
toolchains = ["go"]
go_module = "."

[entrypoints]
example-check = ["go", "-C", "${PACKAGE_ROOT}", "run", "."]
```

The declared directory must contain both `go.mod` and `go.sum`; a `go run`
entrypoint without this declaration is invalid package metadata. Module roots
cannot live below package paths excluded from the immutable content digest
(`.git`, `__pycache__`, or `target`). Local `replace` directives use Go's
single-line or block form and may use a double-quoted path without escapes;
every replacement module is checked recursively. An absolute target, a target
outside the evaluator package, a target below a digest-excluded component, or
an opaque replacement syntax is rejected with the declaring `go.mod` line and directive.
This keeps every source file that can affect the module graph inside the package
digest and therefore inside its cache identity. During trusted environment
preparation, VibeSys downloads the complete module graph into an
operator-owned cache keyed by the immutable evaluator-package digest, then
verifies the graph with proxy and checksum-database access disabled. Docker
builds a missing cache in a short-lived sandbox using the exact target image;
local and remote environments prepare it through their existing pre-readiness
setup boundary. Docker revalidates reused cache contents offline in the target
image before every mount. Slurm stages the verified cache as a read-only
support tree.

Every trusted command from that package, including Python or shell wrappers
that launch Go children, receives the prepared `GOMODCACHE` and runs with
`GOPROXY=off`, `GOSUMDB=off`, `GOWORK=off`, `GOTOOLCHAIN=local`, and
`-mod=readonly`. Candidate commands do not inherit this policy. This is why the
hotel-correctness wrapper can compose the exact resolved microservice package
through a temporary modfile without downloading dependencies when its gate
starts.

## Cargo tools from Git

An evaluator package may declare a Rust CLI installed from an immutable Git
revision:

```toml
[tools.request-factory]
kind = "cargo-git"
git = "https://github.com/uw-syfi/request-factory"
rev = "89dce4a64ae12e7084fbe464ddda882fa0a4c482"
package = "req-frontend"
bins = ["session_runner"]

[entrypoints]
run = ["${TOOL:request-factory/session_runner}"]
```

The revision must be a full lowercase commit SHA. VibeSys always passes
`--locked` to Cargo and installs only the declared binaries. A tool token must
occupy one complete entrypoint argument and may reference only a binary from
the corresponding declaration.

VibeSys installs tools in the environment that executes the evaluator. A
host install looks for `cargo` only when a tool is not already installed and
verified, first on the `PATH` of the user's login shell (the lookup that finds
provider CLIs), then on the process's own, so a run started over a plain `ssh`
command or by the desktop helper finds `~/.cargo/bin` as a terminal run does.
When none has `cargo`, the install fails naming the tool, the `PATH` it searched
and the fix (install Rust for the user, or source `~/.cargo/env` from `~/.profile`). Local
runs use an operator-owned cache outside the candidate workspace. Docker uses
a short-lived sandbox with the target image to build and verify tools, then
mounts only the selected installation roots read-only into the agent sandbox.
The Docker cache key includes the resolved local image ID, so changing a mutable
tag does not reuse a binary built against the previous image. Both the builder
and final sandbox use that immutable image ID.
Modal installs inside the deployed serving container's per-evaluation staging
directory, and SkyPilot installs in the remote job before invoking the
evaluator. Every location uses the same content-addressed plan, normalized
receipt, and binary SHA256 verification. Modal and SkyPilot exclude reserved
evaluator package, tool, and toolchain paths from candidate staging and replace
the tool, toolchain, and bootstrap directories during setup. Cargo runs from a
fresh working directory and ignores candidate Cargo, Rust, and Git configuration
overrides. Remote Go evaluator launches use only the prepared offline module cache, and
the queue evaluator copies its trusted Rust helper to an isolated build directory
before invoking Cargo.

Modal setup and evaluation remain colocated with the deployed service. The
framework owns setup ordering and staged inputs, but Modal's container-exec API
does not add a separate process-identity or mount boundary from the live service.

The target must provide Python 3, `tar`, and the system facilities
needed to install the declared tool. Cargo Git tools cause VibeSys to install a
compatible Rust toolchain when necessary, then run `cargo install` at the
pinned revision with `--locked`. Target images and remote clusters therefore
need outbound package access and a working native linker during first-time
setup. A cargo-git tool declares the Debian/Ubuntu packages its build needs in
`system_packages` (the pinned Request Factory revision uses `openssl-sys` and
declares `pkg-config` and `libssl-dev`). A Docker target installs exactly those
with `apt-get` as root before building, so the agent image need not carry them.
Rootless remote targets and the local host cannot install packages and must
already provide the declared ones.

The bundled Request Factory evaluator exposes three entrypoints. The low-level
`request-factory-engine` entrypoint resolves directly to the pinned binary.
Experiments that need task-owned orchestration use `request-factory-adapter`
and pass a Python script as the first argument; the evaluator invokes that
script with `--request-factory-engine <trusted-path>` before the task's
remaining arguments. It also exports
`VIBESYS_REQUEST_FACTORY_FIXED_TEXT_DRIVER` with the installed path of the
versioned fixed-text driver, so model-specific preparation wrappers can
delegate the generic workload and result behavior instead of copying it.
Fixed independent `/v1/completions` workloads use
`request-factory-fixed-text-v1`; its typed CLI owns deterministic trace and
corpus generation, strict RF summary validation, and protocol-v2 metric output.
The version suffix keeps this workload and result contract explicit while the
generic adapter remains backward compatible.
