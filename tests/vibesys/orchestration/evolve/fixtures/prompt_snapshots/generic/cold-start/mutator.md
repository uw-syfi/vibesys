
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
  `go run ./_evaluator/queue/cmd/accuracy --candidate ./queue-candidate.so --help`; do not guess. The help invocation is
  informational, so its exit status is not a correctness result.
- Benchmark command: `go run ./_evaluator/queue/cmd/benchmark --candidate ./queue-candidate.so`. Use it for a short sanity run and
  discover supported flags with `go run ./_evaluator/queue/cmd/benchmark --candidate ./queue-candidate.so --help`.
  The help invocation is informational, so ignore its exit status.


## Bootstrap the first passing seed

There is no passing parent yet. Build the smallest correct initial candidate from
the reference at `/workspace/reference` and the contracts in the input bundle.
Prioritize an end-to-end passing implementation over optimization; later
generations will mutate it.

### Lessons from 1 failed bootstrap attempt(s)

Do not repeat these failures:

1. The prior candidate violated the documented ABI.



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
