# Queue Optimization Repository

This repository-shaped fixture contains a naive Rust bounded queue and four
VibeSys tasks under `.vibesys/tasks`: `spsc`, `mpsc`, `mpmc`, and the
pure-Rust `verus-mpmc-open` proof experiment.

Run VibeSys from this directory and select one task. The coding agent works in
the repository root, while the selected task supplies its objective,
correctness gate, and benchmark.

The fixture is stored inside the VibeSys repository, so it cannot include its
own nested `.git` directory. `vibesys example new data-structures/repositories/queue-rs DEST`
copies it into a standalone Git repository to run from.

The three native tasks build `queue-candidate.so` and use the versioned queue
evaluator. `verus-mpmc-open` instead targets the `verus-mpmc/` Rust library
directly and requires a matching Verus release on `PATH`.
