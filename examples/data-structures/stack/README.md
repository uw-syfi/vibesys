# Stack Optimization Project

This project holds a naive Rust bounded stack and one VibeSys task per workload under
`.vibesys/tasks/`. The coding agent edits the project root; the selected task
supplies its objective, correctness gate, and benchmark.

| Task | Workload |
| --- | --- |
| `spsc` | single producer, single consumer |
| `mpsc` | multiple producers, single consumer |
| `spmc` | single producer, multiple consumers |
| `mpmc` | multiple producers and consumers, reservation-aware capacity |

Every task loads `./stack-candidate.so`, which `make` builds from
`src/lib.rs`, an intentionally naive candidate using one mutex and `Vec`. The tasks
evaluate it with the versioned `vibesys-evaluator-stack` package
(`resources/evaluators/stack` in the VibeSys repository).

The project lives inside the VibeSys repository, so runs start from a copy.
`vibesys example new` copies it into a new Git repository and prints the
command to run:

    vibesys example new data-structures/stack ~/work/stack
    cd ~/work/stack
    vibesys --task spsc
