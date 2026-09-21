# Open Verus MPMC LIFO Task

This task explores proof-carrying generation of a concurrent stack in Rust and
Verus. It uses the candidate library at `verus-mpmc/` directly, with no C ABI or
shared-library adapter.

The accuracy gate performs three checks:

1. `cargo check --locked` compiles the candidate as ordinary Rust.
2. `cargo verus verify --locked` verifies every opted-in candidate module.
3. The task-owned `accuracy/` crate checks the fixed public interface, sequential
   boundary behavior, LIFO order across non-overlapping producer operations,
   and concurrent-consumer conservation.

The separate task-owned `benchmark/` crate contains only the native producer and
consumer workload and reports `total_ops_per_sec`. Neither crate is part of the
verified candidate, and neither uses the native stack evaluator's C ABI.

Before verification, the runner rejects changes to every fixed file in the
candidate crate, including the manifest and lockfile, module wiring, contract,
facade, README, and ignore rules. Implementations may add or change only regular
Rust source files below `src/candidate/`. The runner also rejects symlinks,
out-of-tree source mechanisms, conditional-compilation splits, and common proof
bypasses such as assumptions, admits, axioms, and external bodies. This is a
fail-closed prototype policy, not a complete adversarial source validator.

The task-local `Dockerfile` includes Git, build tools, Python, and the
verification toolchain. It pins the Ubuntu base image by digest, Verus
`0.2026.08.30.b432e82` and its release archive checksum, rustup `1.28.2` and its
checksum, and Rust `1.97.1`. The Verus release is x86-only, so the Dockerfile
selects `linux/amd64` explicitly.

Run the task from the `stack-rs` repository root:

```bash
vibesys --outer-loop agent \
  --task verus-mpmc-open \
  --runs-dir /absolute/path/to/vibesys-runs --local \
  --backend cpu --profiler none \
  --max-rounds 4
```

From the VibeSys source checkout root, the equivalent command is:

```bash
uv run vibesys \
  --outer-loop agent \
  --project examples/data-structures/repositories/stack-rs \
  --task verus-mpmc-open \
  --runs-dir /absolute/path/to/vibesys-runs --local \
  --backend cpu --profiler none \
  --max-rounds 4
```

Docker with `linux/amd64` support is the only host prerequisite for that
workflow. VibeSys detects the conventional task Dockerfile, builds it with the
task directory as its context, and automatically runs both agents and gates in
the resulting image. Docker layer caching makes subsequent launches
incremental. No separate `docker build`, `--docker`, or `--docker-image` step is
required.

For gate iteration inside an equivalent environment, invoke `runner.py`
directly. The runner stages the immutable accuracy or benchmark crate under
`target/` before compiling it, so Cargo never writes into `.vibesys/`.

```bash
python3 .vibesys/tasks/verus-mpmc-open/runner.py check
python3 .vibesys/tasks/verus-mpmc-open/runner.py check-fixture
python3 .vibesys/tasks/verus-mpmc-open/runner.py benchmark \
  --duration-seconds 1 --output-json results.json
```

The fixed `LifoToken<T>` and logically atomic operations own the abstract
`Seq<T>` transitions, with the top at the end of the sequence. The facade
passes each `AtomicUpdate` to the candidate, which owns synchronization,
representation invariants, operation bodies, and the physical point that
resolves the update. The facade itself contains no runtime synchronization
policy.

`check-fixture` verifies a task-owned alternate implementation with the same
coarse lock but different physical linearization points. Its successful push
and pop operations resolve their atomic updates before mutating the concrete
`Vec`, while still holding the candidate-owned write lock. This guards against
accidentally fixing the seed implementation's update placement in the trusted
interface.
