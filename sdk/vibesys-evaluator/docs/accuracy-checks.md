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
- **Decode strictly.** A body that does not parse, a field the schema does
  not have, a status the operation never answers, an answer that is wrong
  without any state (an id outside the candidates) is a `Violation`: the check
  fails without a search.
- **Statuses.** The recommended default: a 2xx is a known outcome; a 4xx the
  operation can answer (409 full, 404 absent) is a known outcome too, and any
  other 4xx is a `Violation`; a 5xx, a timeout or a reset (`Call.Lost`) is an
  **unknown outcome**: the operation may or may not have taken effect, so its
  piece is `Unknown`.
- **Operations that touch nothing.** A piece with no `Keys` touches no
  state: a lost login, a request rejected before it reached any data. It is
  counted, an unknown one toward `MaxLostShare` too, and never searched. Add
  one rather than skipping the call, so that lost responses count whichever
  step lost them.
- **Several views.** When one projection cannot see every rule (per author and
  per reader, say), give several specs over the same history; all must hold.

### Whole-history facts

Some pieces need a fact about the whole history: a listing or an index
query that found nothing is projected onto every key the history ever
mentions (a key first created after the listing included, since the
listing must not show it yet); a create whose response was lost and whose
id the server generates may have made any id no acknowledged create
claims; a timeline needs every name, mention and follow. `Project` may
range `phases` more than once, and each pass reads the history again,
phase by phase: collect the facts in a first pass and add the pieces in a
second. Memory stays at one phase plus the facts.

```go
Project: func(phases iter.Seq[[]check.Call], out *check.Projection[In, Out]) error {
    keys := map[string]map[string]bool{} // table -> every key the history mentions
    for calls := range phases {          // pass 1: facts
        for i := range calls {
            for _, k := range mentioned(&calls[i]) {
                add(keys, k)
            }
        }
    }
    for calls := range phases {          // pass 2: pieces
        for i := range calls {
            c := &calls[i]
            if isListing(c) {
                got := listed(c) // key -> entity in the answer
                for k := range keys[table(c)] {
                    e, ok := got[k]
                    out.Add(check.Piece[In, Out]{Keys: []string{k}, In: member(c), Out: Out{Present: ok, Ent: e}, Origin: c})
                }
                continue
            }
            ...
        }
    }
    return nil
}
```

Keep facts small (ids, not entities or bodies), and never `Call`s: a phase's
bodies are released when the next phase is read. Each spec's `Project`
reads the history on its own; facts that several specs share can be
collected once in `Main`'s oracle function, ranging `h.Phase(i)` over
`h.Phases`, and closed over by the specs.

### The model

`Init` is a partition's initial state (seeded data comes from the dataset).
`Step` returns the states the system may be in after an operation, or none
and a sentence saying why the answer is impossible; that sentence is what a
failure report shows. Return several states when the model allows several
outcomes: an unknown outcome (applied or not), or a race the application
really has. Train Ticket creates with find-then-save, so two concurrent
creates of one id can both succeed:

```go
case "create":
    if _, exists := s.entities[op.In.ID]; exists && op.Out.Created {
        // Both creates may have found the id absent before either saved it:
        // a second "created" is allowed while the first one is in flight.
        if s.inFlight(op.In.ID, op.Call) {
            return []State{s.with(op.In.ID, op.In.Entity)}, ""
        }
        return nil, fmt.Sprintf("%s already exists, yet the create succeeded", op.In.ID)
    }
```

(The state remembers recent creates with their times, as `Window` does for
values, and prunes them at `Op.Floor`.) States are values: `Step` returns new ones and never mutates its
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
`Fault(state, keys, fault)` returns the states of the partition over `keys`
(the same keys `Init` got) after one; leave it nil when faults must not
change anything (acknowledged writes are durable). With `Fault` nil
the checker does not copy faults into partitions, which matters with
thousands of keys; with `Fault` set, every partition sees every fault. A
fault while an operation was in flight is a capture problem and makes the
verdict UNKNOWN.

A restart can change state on purpose. Train Ticket's services re-apply their
seed data on startup (create-if-absent for most tables, an upsert for routes
and prices), so the model closes over the pristine seed and re-applies it to
the partition's keys:

```go
seed := readSeed(h.Dataset) // in Main's oracle function
spec.Fault = func(s State, keys []string, f check.Fault) []State {
    if f.Kind != "restart" {
        return []State{s}
    }
    for _, k := range keys {
        e, seeded := seed.entity(k)
        if !seeded || !seed.ofService(f.Target, k) {
            continue
        }
        if _, present := s.get(k); !present || seed.upserts(f.Target) {
            s = s.with(k, e)
        }
    }
    return []State{s}
}
```

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
- Keep unknown outcomes near zero: the accuracy load is fixed and low. More
  than `MaxLostShare` (default 1%) of pieces with an unknown outcome (lost
  responses and 5xx alike: whatever the projection marked `Unknown`) fails the
  check, since at that load they are the candidate's fault. A task can raise
  it with `--max-lost-share`.
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

## Re-checking old runs

Runs captured before the accuracy capture wrote history directories are
loadgen phase directories (`results.binpb` or `results.binpb.gz`,
`summary.binpb`, the generator's `kept.binpb` or `state/kept.binpb`). Every
oracle's command imports them, with the faults between them, and checks the
result like any history:

```sh
oracle import --out /tmp/h --dataset run/dataset.json \
    --fault kind=restart,target=frontend,after=0 \
    --fault kind=flush,target=memcached,after=1,start=1762412345000000000 \
    run/phase-0 run/phase-1 run/phase-2
oracle --history /tmp/h --out /tmp/report
```

A fault without times is placed between the last response of its phase and
the first call of the next. The phases must have kept a record of every
operation they sent (the accuracy capture's setting); phases that kept only
writes and sampled reads cannot be checked.

## Reading a report

The first line is the verdict and its reason; then, per spec, the coverage
(calls, lost calls, pieces, unknown outcomes, pieces without keys,
partitions, ops checked and unchecked, reads sampled out, the most ops in
flight at once), warnings, the partitions (undecided and failing first) and
the failures.

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
