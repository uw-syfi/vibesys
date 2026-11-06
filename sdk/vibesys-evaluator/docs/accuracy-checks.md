# Writing an accuracy oracle

An accuracy check runs a candidate under a fixed, low load, records every
operation it answered, and asks whether those answers are ones the task's
semantics allow. The semantics are the task's **oracle**: one or more
`check.Spec`s, written in Go against the package
`github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check` (listing:
[`api/go-check.txt`](../api/go-check.txt); the design is in
[`spec.md`](spec.md#accuracy-checks-gocheck)). The oracle lives in the task's
repository, next to its workload.

The checker decides **linearizability per key**: for each group of keys it
looks for an order of the operations on them that respects real time (an
operation that returned before another started comes first) and in which
every answer is what the oracle's model says. If such an order exists for
every group, the check passes. The search is porcupine's, with budgets.

## The pieces of an oracle

```go
spec := &check.Spec[State, In, Out]{
    Name:    "booking",
    Project: project,             // calls -> pieces and violations
    Init:    func(keys []string) State { ... },
    Step:    step,                // (state, op) -> next states, or a reason
    Fault:   nil,                 // restarts and flushes change nothing
    Equal:   func(a, b State) bool { ... },
    Hash:    func(s State) uint64 { ... },
    EffectWindow: time.Second,    // a lost write lands within 1 s or never
}
func main() {
    check.Main(func(h *check.History) ([]check.AnySpec, error) {
        data, err := readDataset(h.Dataset)
        ...
        return []check.AnySpec{spec}, nil
    })
}
```

`check.Main` is the task's `[accuracy] command`: it reads the captured
history directory, runs the check, writes `report.txt` and HTML timelines to
`/vibesys/artifacts/accuracy/`, and exits 0 (PASS), 1 (FAIL) or 2 (UNKNOWN).
Run the same binary on a copied history directory to debug a verdict
offline.

### Projection

`Project` ranges over the history's phases and turns each `Call` (one
operation: its name, the workload's `Info`, its steps with status and body,
its start and end) into `Piece`s. A piece names the **keys** it touches, its
input and output, whether it is a `Write`, whether its outcome is `Unknown`,
and its `Origin` call.

- **Project onto keys.** An operation that reads several keys (a search, a
  listing, a timeline) becomes one piece per key it could have returned, each
  checked in that key's history. Promise per-key views, not snapshots of a
  whole collection: an application that answers a listing from several
  replicas or caches is correct under per-key rules and fails a snapshot rule.
- **Several keys in one piece** mean the operation is atomic over them: the
  checker then searches those keys together (their connected component).
  Use this only for what the contract makes atomic; one hot multi-key
  operation can merge everything into one huge partition.
- **Decode strictly.** A body that does not parse, a status the operation
  never answers, an answer that is wrong without any state (an id outside the
  candidates) is a `Violation`: the check fails without a search.
- **Lookups across phases** (an id created in phase 0, read in phase 2) are
  resolved by keeping a small index across loop iterations and adding the
  dependent pieces after the loop. Do not keep `Call`s: a phase's bodies are
  released when the next phase is read, and only the pieces stay in memory.
- **Several views.** When one projection cannot see every rule (per author and
  per reader, say), give several specs over the same history; all must hold.

### The model

`Init` is a partition's initial state (seeded data comes from the dataset).
`Step` returns the states the system may be in after an operation, or none
and a sentence saying why the answer is impossible; that sentence is what a
failure report shows. Return several states when the model allows several
outcomes. States are values: `Step` returns new ones and never mutates its
argument. `Equal` and `Hash` define when two states are the same for the
search's cache; states that differ only in how they were reached should
compare equal, or the search cannot merge them.

Building blocks for states:

- `Register[V]`: one item under bounded staleness, in linearization order. A
  read that started at `c` may show any version not certainly overwritten
  more than the bound before `c`. With bound 0 it is a linearizable register.
- `Window[V]` and `Counter`: order-insensitive relaxations for items that
  concurrent clients write often (a name, a price, units taken). They allow
  everything a `Register` allows, so a correct history stays valid, and their
  states do not depend on the order of overlapping writes, which keeps the
  search small. A violation that only the order of overlapping writes reveals
  can be missed.
- `PMap[V]`: a persistent map for states with many items (units per slot),
  so a step does not copy them all.
- `Op.Floor`: every operation the search may still place after this one was
  called at or after `Floor`. Prune what no such operation can observe
  (`Register.Prune`, `Window.Prune`, `Counter.Prune`) to keep states small and
  comparable.

**Model the application as it is.** The DeathStarBench hotel spike found
behaviour the old oracle missed: multi-night stays give rooms back night by
night (not atomically), capacity changes race with reservations, and the
pinned frontend reserves rooms even when the login failed. An oracle that
assumes the cleaner contract fails the base application.

### Faults

Restarts and cache flushes happen between phases, at quiescent points.
`Fault(state, fault)` returns the states after one; leave it nil when faults
must not change anything (acknowledged writes are durable). With `Fault` nil
the checker does not copy faults into partitions, which matters with
thousands of keys. A fault while an operation was in flight is a capture
problem and makes the verdict UNKNOWN.

## Unknown outcomes and EffectWindow

An operation whose response was lost (`Call.Lost`: a timeout, a reset) may or
may not have taken effect: mark its piece `Unknown`, and `Step` returns both
states. Unknown outcomes are the main cost of a search: each one stays open
until its effect window ends and doubles the states the search tracks while
it is open. In the spikes, 5% lost writes made even 8-client histories
undecidable. So:

- Set `EffectWindow` to how long a lost operation can still take effect (the
  application's own request deadline). Without it, a lost write can take
  effect any time until the end of the history.
- Keep lost responses near zero: the accuracy load is fixed and low. More
  than `MaxLostShare` (default 1%) of pieces with an unknown outcome fails the
  check, since losing responses at that load is the candidate's fault. A task
  can raise it with `--max-lost-share`.
- An unknown read tells nothing and is dropped.

## Keep per-key overlap low

The search accepts a correct history in about one step per operation. A
history it must reject is expensive: before failing, it tries the orders of
the operations in flight together, about 2^overlap per operation. Measured
on one hot key with 4,000 operations, a 1 GiB heap budget and a 60 s
partition budget:

| ops in flight on the key | correct history | history with a race |
| --- | --- | --- |
| 4 | 39 ms | 25 ms |
| 8 | 65 ms | 194 ms |
| 16 | 97 ms | 40 s, 970 MB |
| 32 | 1.1 s | undecided (memory) |
| 64 | undecided (time) | undecided (memory) |

Keep the most operations of one key in flight at once at about 12 or fewer:
fewer clients or a lower rate per key in the accuracy workload, or more keys.
The report gives the maximum (`MaxInFlight`) and warns above 12. An
undecided partition is more likely than others to hide a violation, because
rejecting is what is expensive.

## Sampling and budgets

`Options` (and `Main`'s flags) bound a check:

- `MaxReadsPerKey` (5,000) keeps a seeded uniform sample of each key's reads;
  writes are never sampled. Sampling can hide a violation, never invent one;
  the report counts what it dropped.
- `PartitionTimeout` (60 s) and `TotalTimeout` (10 min) bound the searches;
  `MaxSearchSteps` bounds a search in model steps, independent of the
  machine.
- `MemoryBytes` (2 GiB) bounds the checker's heap. Above it the search with
  the most steps is aborted; when all partitions are done, aborted ones are
  retried alone. Peak RSS runs about 1.3 times the heap budget; size the pod
  for that.
- `MaxOpsPerPartition` (50,000): a larger partition is not searched.

What a check costs in practice: a real DeathStarBench hotel run of four
20-second phases at 16 clients (614,000 operations, 1.84 million pieces, 84
partitions) checked in 28 s with a 2.2 GB peak heap; a social network run of
171,000 calls spent 12.6 s projecting and 0.3 s searching. Memory, mostly the
projected pieces, is the practical limit: project small inputs and outputs.

## Reading a report

The first line is the verdict and its reason; then, per spec, the coverage
(calls, lost calls, pieces, unknown outcomes, partitions, ops checked and
unchecked, reads sampled out, the most ops in flight at once), warnings, the
partitions (undecided and failing first) and the failures.

A failure names its partition and the **first violation**: the earliest
response after which no order fits. The checker finds it by searching
prefixes of the partition's history (calls still in flight at the cut count
as unknown), so the operations it names are where the history first goes
wrong, not where the search gave up. It shows a state the history before that
response can end in, the model's sentence for why the response cannot follow
it, the partition's other operations from 1 s before, and nearby faults. The
HTML timeline (`<spec>-failure-<n>.html`) is porcupine's: hover over an
operation to see the states the search reached.

UNKNOWN is never a pass. Its reason names the largest undecided partition,
why it was undecided (time, memory, too large) and what to change: fewer ops
in flight on the key, an `EffectWindow`, a larger budget, less history, or
states that compare equal.
