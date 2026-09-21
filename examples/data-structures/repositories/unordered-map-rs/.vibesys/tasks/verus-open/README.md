# Open Verus Unordered Map Task

This task explores proof-carrying generation of a concurrent unordered map in
Rust and Verus. It uses the candidate library at `verus-map/` directly, with no
C ABI or shared-library adapter.

The accuracy gate performs three checks:

1. `cargo check --locked` compiles the candidate as ordinary Rust.
2. `cargo verus verify --locked` verifies every opted-in candidate module.
3. The task-owned `accuracy/` crate checks the fixed public interface, sequential
   put/get/remove, concurrent disjoint inserts, and concurrent-remove
   conservation.

The separate task-owned `benchmark/` crate runs a mixed 4-client workload
(70% get, 20% put, 10% remove) over a 256-key universe and reports
`total_ops_per_sec`. Neither crate is part of the verified candidate.

Before verification, the runner rejects changes to every fixed file in the
candidate crate. Implementations may add or change only regular Rust source
files below `src/candidate/`. The runner also rejects symlinks, out-of-tree
source mechanisms, conditional-compilation splits, and common proof bypasses.

The task-local `Dockerfile` pins Ubuntu by digest, Verus
`0.2026.08.30.b432e82`, rustup `1.28.2`, and Rust `1.97.1`.

Run the task from the `unordered-map-rs` repository root:

```bash
vibesys --outer-loop agent \
  --task verus-open \
  --runs-dir /absolute/path/to/vibesys-runs --local \
  --backend cpu --profiler none \
  --max-rounds 4
```

From the VibeSys source checkout root, the equivalent command is:

```bash
uv run vibesys \
  --outer-loop agent \
  --project examples/data-structures/repositories/unordered-map-rs \
  --task verus-open \
  --runs-dir /absolute/path/to/vibesys-runs --local \
  --backend cpu --profiler none \
  --max-rounds 4
```

Docker with `linux/amd64` support is the only host prerequisite for that
workflow. VibeSys detects the conventional task Dockerfile, builds it with the
task directory as its context, and automatically runs both agents and gates in
the resulting image. No separate `docker build`, `--docker`, or `--docker-image`
step is required.

```bash
python3 .vibesys/tasks/verus-open/runner.py check
python3 .vibesys/tasks/verus-open/runner.py check-fixture
python3 .vibesys/tasks/verus-open/runner.py benchmark \
  --duration-seconds 1 --output-json results.json
```

The fixed `MapToken` and logically atomic operations own the abstract unique-key
sequence. The facade passes each `AtomicUpdate` to the candidate. The facade
itself contains no runtime synchronization policy.

`check-fixture` verifies a task-owned alternate implementation with the same
coarse lock but different physical linearization points: successful put and
remove resolve their atomic updates before mutating the concrete `Vec`.
