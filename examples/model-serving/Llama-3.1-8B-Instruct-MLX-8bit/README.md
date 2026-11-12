# Llama-3.1-8B-Instruct-MLX-8bit: JSON-schema generation on a Mac

VibeSys input bundle: minimize the end-to-end latency of an OpenAI-style
`/v1/completions` server for `mlx-community/Meta-Llama-3.1-8B-Instruct-8bit`
that answers with JSON valid for a schema from
[JSONSchemaBench](https://huggingface.co/datasets/epfl-dlab/JSONSchemaBench).
See [`OBJECTIVE.md`](.vibesys/tasks/default/OBJECTIVE.md) for the objective and
the server contract.

The task is written for `vibesys-cli` (task schema 0.2) and runs on the host: the
Metal backend cannot run in a container. The legacy Python app refuses it.

## Layout

```
Llama-3.1-8B-Instruct-MLX-8bit/
├── pyproject.toml, uv.lock   # the candidate's environment (mlx-lm, llguidance)
├── engine/                   # the candidate: a working baseline the agent optimizes
│   ├── server.py             # HTTP: GET /health, POST /v1/completions (SSE)
│   ├── generate.py           # mlx-lm generation on one thread
│   └── constraint.py         # llguidance token masks applied to the MLX logits
└── .vibesys/tasks/default/   # trusted task files; read-only for the agent
    ├── vibesys.input.toml    # manifest: check and benchmark commands
    ├── objectives.toml       # headline metric: latency_ms.p50, lower is better
    ├── OBJECTIVE.md          # what to optimize, the launch and server contracts
    ├── pyproject.toml, uv.lock  # the judge's environment (vibesys-evaluator, httpx, jsonschema, tokenizers)
    ├── bench.py              # check, benchmark and profile, on vibesys_evaluator.main
    ├── client.py             # one streaming request, timed; reads no finish_reason
    ├── launch.py             # starts the candidate's server, waits for /health, stops its group
    └── workload.py           # which schemas, the prompt, the sentinel, the gate
```

## Run it

Build `vibesys-cli` (`cd cli && go build -o vibesys-cli ./cmd/vibesys-cli`, or
install a release as `cli/README.md` describes), commit the project to a git
repository with the example's files at its root, and submit jobs for a commit:

```bash
vibesys-cli submit check HEAD --task default --wait
vibesys-cli submit measure HEAD --task default --samples 3 --no-plan --wait
vibesys-cli show HEAD --task default
```

The machine needs `uv` and about 9 GiB of disk for the weights. The first job
downloads the pinned model and dataset into the Hugging Face cache
(`~/.cache/huggingface`) and installs both environments with `uv`; later jobs
find them there. Each job starts its own server, so a job pays the model load
(about 10 s) once, and the benchmark does not count it.

Each piece by hand, from the project root, with the judge's environment and a
free port:

```bash
uv run --project . python -m engine.server --model-dir <snapshot> --port 8000
uv run --frozen --project .vibesys/tasks/default python .vibesys/tasks/default/bench.py check
```

## What the task asks

The `val` split of the JSONSchemaBench dataset's default config (the dev split, which
scores the loop), pinned to
revision `5bd0f4640badc6f3f02df796421d21cb0ca0b141`, without the schemas above
1500 characters (444 of 937 remain; the cap is about time, not about what a
decoder supports, see `OBJECTIVE.md`). A fixed pseudo-random order picks the
schemas: ranks 0 to 23 for the benchmark and ranks 24 to 63 for the check, so
they share none. Each request's sentinel is drawn at run time, never from the
schema or a seed. The model is pinned to revision
`142d428004044c37c441272c91316251d9aecc58`.

- **Check.** No request fails, every answer that ended validates against its
  schema, at most 10% of the answers are cut off at 512 tokens, and at least
  80% of the answers that ended, to schemas that can hold a string, include the
  sentinel from the prompt. The judge decides itself what was cut off: an
  invalid answer of at least 320 tokens by its own count, with the pinned
  model's tokenizer, was; a shorter one ended. It never reads the server's
  `finish_reason`. The baseline's answers: 40 answers, 39 valid, 1 cut off, none that ended invalid, the sentinel in 30 of 34. The cut-offs are
  objects whose properties the model wrote in another order than the grammar
  allows, and a string it never closes.
- **Benchmark.** One request that is not counted, then 24 requests one after
  another. `latency_ms.p50` is the median time from sending a request to its
  last chunk. A sample is a measurement only if every request completes and the
  answers pass the check's gate.
- **Holdout.** The benchmark's measurement and gate on the dataset's `test`
  split (1400 schemas of at most 1500 characters; the first 96 of the fixed
  order are asked), with validity broken down by the schema's source. Run it on
  a final or promoted commit, never in the loop (no manifest section runs it):
  `vibesys-cli submit command <commit> --image harness -- uv run --frozen --project
  .vibesys/tasks/default python .vibesys/tasks/default/bench.py holdout`. It
  takes about 7 minutes with the baseline (48 schemas took 3.4 minutes). It reports `latency_ms.p50` and the
  other benchmark metrics, plus `valid_frac.<source>` for each of `github`,
  `glaive`, `kubernetes`, `snowplow`, `washingtonpost` and `store` the subset
  contains (the source is read off the id's shape, as the parquet has no such
  column), and fails as the benchmark does when the answers are not correct.
  The records are kept as `holdout.jsonl` in the job's artifacts.
- **Profile.** `bench.py profile` asks 4 requests and keeps the server log and
  answers with the job. Run the mode as a command job under a profiler's wrapper:
  `vibesys-cli submit command HEAD --image harness --wrap '<profiler>' -- uv run --frozen
  --project .vibesys/tasks/default python .vibesys/tasks/default/bench.py profile`.

The weights and the dataset come from the Hugging Face cache and are
verified by revision, not by content.

## The baseline

`engine/` is a minimal, honest starting point, not an optimized server. It loads
the model with mlx-lm and serves one request at a time. For a request with a
`json_schema` response format it compiles the schema to a grammar with
[llguidance](https://github.com/guidance-ai/llguidance) and masks, at every
step, the tokens the grammar does not allow, using llguidance's Metal kernel for
the mask. The schema is compiled with compact output (no optional whitespace),
`oneOf` read as `anyOf`, and unsupported keywords skipped; the checker holds the
answer to the whole schema. The prompt is the user turn of the model's chat
template.
