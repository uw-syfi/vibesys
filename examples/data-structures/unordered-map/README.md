# Unordered Map Optimization Project

This project holds a naive Rust concurrent unordered map and one VibeSys task per workload under
`.vibesys/tasks/`. The coding agent edits the project root; the selected task
supplies its objective, correctness gate, and benchmark.

| Task | Workload |
| --- | --- |
| `mw` | multiple writers |
| `swmr` | single writer, multiple readers |

Every task loads `./unordered-map-candidate.so`, which `make` builds from
`src/lib.rs`, an intentionally naive candidate using one mutex and `HashMap`. The tasks
evaluate it with the versioned `vibesys-evaluator-unordered-map` package
(`resources/evaluators/unordered-map` in the VibeSys repository).

The project lives inside the VibeSys repository, so it cannot hold its own
`.git` directory. Copy it to a new directory, make that copy a Git repository,
and run one task there:

    vibesys --task mw
