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

The local collection is the initial package source. Published packages can
later provide the same metadata and entry-point names through a registry-backed
resolver. Repository-specific checks and workloads belong with their task, not
in this directory.

## Cargo tools from Git

An evaluator package may declare a Rust CLI installed from an immutable Git
revision:

```toml
[tools.request-factory]
kind = "cargo-git"
git = "https://github.com/uw-syfi/request-factory"
rev = "118da6137275fda3a290e9012853214dc437c6c0"
package = "req-frontend"
bins = ["session_runner"]

[entrypoints]
run = ["${TOOL:request-factory/session_runner}"]
```

The revision must be a full lowercase commit SHA. VibeSys always passes
`--locked` to Cargo and installs only the declared binaries. A tool token must
occupy one complete entrypoint argument and may reference only a binary from
the corresponding declaration.

VibeSys installs tools in the environment that executes the evaluator. Local
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
overrides. Remote Go evaluator launches disable parent `go.work` discovery, and
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
setup. They must also provide any native build dependencies required by the
declared Cargo package. The currently pinned Request Factory revision uses
`openssl-sys`; Debian and Ubuntu targets need `pkg-config` and `libssl-dev`.

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
