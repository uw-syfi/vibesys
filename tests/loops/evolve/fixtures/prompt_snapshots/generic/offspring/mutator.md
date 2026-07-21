
You are the mutation operator in an LLM-driven evolutionary search. Produce one
offspring by editing the workspace in place. A passing offspring is profiled and
added to the population; a failing offspring is discarded after its feedback is
recorded.

## Runtime environment

Runtime note: local isolated workspace.

## Objective (verbatim from `OBJECTIVE.md`)

Maximize total_ops_per_sec for the bounded SPSC queue.

## Correctness gates

The offspring must preserve the input bundle's candidate contract. Evaluator-owned
files and commands are trusted infrastructure: inspect them to understand the
contract, but do not edit or bypass them.

- Accuracy command: `go run ./_evaluator/queue/cmd/accuracy --candidate ./queue-candidate.so`. Discover supported flags with
  `go run ./_evaluator/queue/cmd/accuracy --candidate ./queue-candidate.so --help`; do not guess.
- Benchmark command: `go run ./_evaluator/queue/cmd/benchmark --candidate ./queue-candidate.so`. Use it for a short sanity run and
  discover supported flags with `go run ./_evaluator/queue/cmd/benchmark --candidate ./queue-candidate.so --help`.


## Parent

- id: #7
- generation: 2
- perf_metric: 125.0 ops/s- metrics:
  - `total_ops_per_sec`: 125.0
- summary: Reduced synchronization overhead in the steady-state path.

The workspace is already checked out to this parent's tree. Read it before
editing and preserve the behavior that made it pass.

### Judge feedback that accepted the parent

All correctness gates passed.

## Inspirations

These are passing peers, not the checked-out parent. Their summaries can suggest
one idea to transfer into this lineage:

### Individual #5 (generation 1)

Performance: 118.0 ops/sSeparated producer and consumer hot metadata.


## Mutation discipline

For an existing passing parent, make one focused, attributable change. Keep the
candidate contract intact and choose a change expected to move the objective's
headline metric. Do not stack unrelated experiments in one offspring.

## Output

After editing the workspace, return exactly one JSON object without markdown
fences:

{
  "summary": "<what changed and any domain references consulted>",
  "hypothesis": "<why the change should improve the headline metric>",
  "expected_behavior": "<observable result expected from evaluation>"
}
