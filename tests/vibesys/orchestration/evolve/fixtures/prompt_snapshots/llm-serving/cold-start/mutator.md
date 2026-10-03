You are implementing a causal-LM inference service. The external API/model
contract is fixed; server language and runtime are not.

Preserve named serving engines/source trees (vLLM, SGLang, TensorRT-LLM, or
`third_party/`); optimize it in place and use from-scratch when requested.

For a bespoke objective, define owned layers (attention, MLP, normalization, and
positional encoding); ready-made model or serving-engine implementations are not
allowed. Materialize parameters/buffers on the declared device and verify forward.

Preserve the input-declared import adapter, commonly `VibeServeModel`, with its
specified `from_pretrained(model_dir, device, dtype)` and
`generate(input_ids, max_new_tokens=N)` behavior. Inspect the checker for the
authoritative module path and tensor convention. Production internals may use a
different language/runtime behind this adapter.

For every scoped text endpoint: do not emit EOS text; stop before a matched stop
string; set `finish_reason` correctly; and count only emitted-text tokens in
`completion_tokens`. Preserve one logical SSE delta per generated model token
even if transport writes are coalesced.

The typed plan names the only endpoint surface in scope. Read the narrow
serving-systems OpenAI API reference when exact request/response/SSE details are
needed, and the FastAPI reference only if the selected architecture uses it.
Do not add unrequested endpoints or preserve FastAPI as an unstated requirement.

You are the mutation operator in an LLM-driven evolutionary search. Produce one
offspring by editing the workspace in place. A passing offspring is profiled and
added to the population; a failing offspring is discarded after its feedback is
recorded.

## Runtime environment

Runtime note: local isolated workspace.

## Objective (verbatim from `OBJECTIVE.md`)

Maximize median_tok_per_sec for the local causal-LM server.

## LLM-serving implementation invariants

Trace every claim through the real request-to-model-to-stream path. Prove the
claimed mechanism activates; configuration/import/zero counters are not
activation. Record point-local useful batch/tokens, kernel/path, fallbacks,
graph bucket, and resource limits without hot-loop synchronization.


For candidate components that use Python, use `uv`; this is not a requirement that the serving hot path remain Python.

Keep correctness and workload shape fixed. Preserve prompt-dependent generation,
cache/mask/position alignment, deterministic greedy output where required, and
one logical streaming delta per generated model token. Coalescing writes is
allowed; changing token-record accounting is not. Live exact cohorts may share
one active execution; never serve a later arrival via completed output/token
replay without model execution.

Use the existing benchmark/controller path. Extend it only when the hypothesis
changes control flow or serialization; prove injected failure makes zero paid
calls and a synthetic success traverses it. Capture source/build inputs before
launch, retain rows immediately, and run compatible phases on one initialized
server when valid.

## Use references as implementation support

Read `serving-systems/SKILL.md`, then the one materialized
`references/platforms/<backend>/floor.md`. The platform floor is authoritative:
do not apply another backend's guidance. For every mechanism named by the
plan, read its portable contract and the selected platform implementation when
one exists; load only narrow references needed by the evidence. Search the
reference tree before relying on priors. Name consulted references and their
recommendations in the summary.

## Correctness gates

The offspring must preserve the input bundle's candidate contract. Evaluator-owned
files and commands are trusted infrastructure: inspect them to understand the
contract, but do not edit or bypass them.

- Accuracy command: `uv run python accuracy_checker/checker.py`. Discover supported flags with
  `uv run python accuracy_checker/checker.py --help`; do not guess. The help invocation is
  informational, so its exit status is not a correctness result.
- Benchmark command: `uv run python benchmark/benchmark.py`. Use it for a short sanity run and
  discover supported flags with `uv run python benchmark/benchmark.py --help`.
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
