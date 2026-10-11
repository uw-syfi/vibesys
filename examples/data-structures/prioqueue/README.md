# Priority Queue Optimization Project

This project holds a naive Rust bounded priority queue and one VibeSys task per workload under
`.vibesys/tasks/`. The coding agent edits the project root; the selected task
supplies its objective, correctness gate, and benchmark.

| Task | Workload |
| --- | --- |
| `spsc` | single producer, single consumer |
| `mpsc` | multiple producers, single consumer |
| `spmc` | single producer, multiple consumers |
| `mpmc` | multiple producers and consumers |

Every task loads `./priority-queue-candidate.so`, which `make` builds from
`src/lib.rs`, an intentionally naive candidate using one mutex and `BinaryHeap`. The tasks
evaluate it with the versioned `vibesys-evaluator-priority-queue` package
(`resources/evaluators/priority-queue` in the VibeSys repository).

The project lives inside the VibeSys repository, so runs start from a copy.
`vibesys example new` copies it into a new Git repository and prints the
command to run:

    vibesys example new data-structures/prioqueue ~/work/prioqueue
    cd ~/work/prioqueue
    vibesys --task spsc
