# Llama 3.2 3B MLX shared-prefix serving

Stock MLX-LM HTTP baseline for an Apple M5 MacBook Air with 16 GB unified memory.
Four concurrent questions share approximately 4096 tokens of readable text with
randomized sentinel facts. One warmup uses the same document; scored questions
use temperature zero and at most 64 generated tokens each. Requests use MLX-LM's
`default_model` alias to reuse the supplied local snapshot.

This is a native VibeSys task, `shared-prefix`. Candidate `server.py` starts stock
MLX-LM 0.31.3 with `--prompt-cache-size 1` instead of its default of 10.
This bounds retained completed-request caches to one entry using the native
cache eviction policy; it does not cap active batch caches or total Metal
memory. The warmup still precedes four concurrent scored requests. Correctness
uses the same server for two changing documents, retaining stale-response
coverage. The exact stock command is recorded in `server.log`, and candidate
source hashes are saved. `server.json.stock_defaults` describes package defaults,
not effective candidate options; use the recorded command for those.
This configured stock baseline must be distinguished
from runs using MLX-LM's default cache size of 10. This README does not report
optimization results.
Held-out checker, benchmark, workload, and reporting programs live
under `.vibesys/tasks/shared-prefix/`. No model weights or submodules are needed
from this repository. The model must already exist in the local cache.

The primary objective is **minimize p50 HTTP TTFT**. The benchmark requires every
answer to be correct and every response to be a complete OpenAI-style SSE stream.
It retains actual completion-token usage, cached-token counts, raw timings, and
server process peak RSS. Stock HTTP prompt-processing time and Metal peak memory
remain explicitly unavailable. RSS does not represent total unified memory.

## Reproduce the workload

Run the commands in this section from the repository root. Set
`MLX_MODEL_PATH` to the absolute path of a local MLX model snapshot, for example
an already-downloaded Hugging Face snapshot directory. The placeholder below is
not a path supplied by this repository.

Set up the serving-only example environment:

```sh
cd examples/model-serving/Llama-3.2-3B-Instruct-MLX-4bit-shared-prefix
export MLX_MODEL_PATH=/absolute/path/to/your/MLX-model-snapshot
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
uv sync --offline --no-dev
```

`uv sync` creates an isolated dependency environment, without loading the model.
An offline dependency-resolution failure is distinct from a missing model.

Optionally confirm model generation:

```sh
uv run --offline --no-dev mlx_lm.generate --model "$MLX_MODEL_PATH" --prompt 'The favorite fruit in this note is Mango. Reply with the fruit only.' --max-tokens 16 --temp 0
```

The answer should contain `Mango`. Start the candidate HTTP server:

```sh
uv run --offline --no-dev python server.py --model-path "$MLX_MODEL_PATH" --host 127.0.0.1 --port 8765
```

In a second VS Code terminal, check its streaming endpoint:

```sh
curl --no-buffer --fail-with-body --max-time 60 http://127.0.0.1:8765/v1/chat/completions -H 'Content-Type: application/json' --data '{"model":"default_model","messages":[{"role":"user","content":"The favorite fruit in this note is Mango. Reply with the fruit only."}],"temperature":0,"max_tokens":16,"stream":true,"stream_options":{"include_usage":true}}'
```

The response must contain `Mango` and terminate with `data: [DONE]`. Stop that
server with Ctrl-C before running the checker. Evaluation starts and owns its
own fresh server on a free localhost port.

### Correctness

Validate the task and run the held-out correctness checker:

```sh
vibesys validate . --task shared-prefix
RUN_TAG="$(date +%Y%m%dT%H%M%S)"
uv run --offline --no-dev python .vibesys/tasks/shared-prefix/accuracy_checker/checker.py --model-path "$MLX_MODEL_PATH" --artifact-dir "artifacts/correctness-${RUN_TAG}"
```

### Baseline

After the correctness checker passes, run the stock baseline with the same
model path:

```sh
uv run --offline --no-dev python .vibesys/tasks/shared-prefix/benchmark/benchmark.py --model-path "$MLX_MODEL_PATH" --artifact-dir "artifacts/baseline-${RUN_TAG}" --vs-output "artifacts/baseline-${RUN_TAG}.result.jsonl" --round 0 --status baseline
```

Only run the benchmark after the checker passes. Artifact paths must be new;
the programs refuse to overwrite existing evaluations. The protocol output is a
sidecar outside the exclusively owned evaluation directory. A baseline is valid
only when all warmup/scored correctness and streaming checks pass and every
required metric is finite. A four-request median is a conservative first
measurement; retain matched-seed repeats separately before claiming small gains.

## Non-Metal checks

Unit tests inject tokenizer, HTTP stream, and clock Fakes. They never import MLX
or load weights. The protocol integration tests use VibeSys repository packages.
Run the full suite from the repository root with its development environment,
rather than the serving-only example environment. If continuing in the shell
used above, return to the repository root first:

```sh
cd "$(git rev-parse --show-toplevel)"
uv run pytest --no-cov examples/model-serving/Llama-3.2-3B-Instruct-MLX-4bit-shared-prefix/.vibesys/tasks/shared-prefix/tests/test_evaluator.py
vibesys validate examples/model-serving/Llama-3.2-3B-Instruct-MLX-4bit-shared-prefix --task shared-prefix
```

Repository contributors also run the registry static/trust tests and root
format/lint scripts. Those checks do not execute the serving workload.

## Design and reporting

The example owns its serving contract and evaluator. Candidate source has no
dependency on VibeSys core. Checker and benchmark depend on the evaluator's
declared public API; reporting consumes saved records. This adds no framework
module edges. The stock server launch hides serving internals from callers;
the evaluator owns all subprocess/network lifecycle effects.

Each evaluation saves exact commands, sanitized environment, package/model and
candidate identity, seed/document/prompt hashes and token counts, expected facts,
response traces, timings, actual token usage, memory scope, and errors. Expected
facts are evaluator artifacts, never sent to the candidate as an answer map.

`reporting/report.py --help` documents generating CSV, SVG, and a short write-up
from saved evaluations. Provide real round numbers for future optimization
measurements; records without an assigned round must not appear as round zero.
Missing/failed/unreviewed performance and unknown agent usage stay explicit.
