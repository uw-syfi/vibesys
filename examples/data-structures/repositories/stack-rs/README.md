# Stack Optimization Repository

This repository-shaped fixture contains a naive Verus-gated bounded stack and
one VibeSys task under `.vibesys/tasks`: the pure-Rust `verus-mpmc-open` proof
experiment.

Run VibeSys from this directory and select that task. The coding agent works in
the repository root, while the task supplies its objective, correctness gate,
and benchmark.

The fixture is stored inside the VibeSys repository, so it cannot include its
own nested `.git` directory. Tests and run setup copy it to an isolated location
and initialize that copy as a standalone Git repository before optimization.

`verus-mpmc-open` targets the `verus-mpmc/` Rust library directly and requires a
matching Verus release on `PATH`. Accuracy is `cargo check`, `cargo verus
verify`, then a task-owned runtime harness. There is no C ABI and no Go
Porcupine checker on this track.
