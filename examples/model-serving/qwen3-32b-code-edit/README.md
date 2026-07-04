# qwen3-32b-code-edit — predicted-outputs benchmark

This input bundle measures **single-batch tok/s** of an OpenAI-style [predicted-outputs](https://developers.openai.com/api/docs/guides/predicted-outputs) server for `Qwen/Qwen3-32B` on the **code-debug** subset of [`m-a-p/CodeEditorBench`](https://huggingface.co/datasets/m-a-p/CodeEditorBench). The benchmark sends the buggy original code as `prediction.content` on every request; the server is expected to consume that prediction as the draft sequence for speculative decoding against the target.

**Single-batch only.** Concurrency 1. The metric the orchestrator tracks is `median_tok_per_sec` from `benchmark/benchmark.py`.

## Why this dataset

CodeEditorBench's "code debug" rows hand the model an `incorrect_solutions` (the buggy code) and ask it to emit `solutions` (the same code with the bug fixed). On Python3 rows, **median character overlap between the two is 99.6%**, and 75% of matched runs are >16 chars long. That is exactly the regime predicted outputs is designed for: the model's output is dominated by long verbatim copies of a known prediction with a few small islands of new tokens.

`benchmark/benchmark.py` records, per sample, the token-level alignment between the actual model output and the prediction (matched-run lengths, longest run, total matched/diverged tokens). Those numbers are what an analytical headroom calculation would feed on.

## Layout

```
qwen3-32b-code-edit/
├── OBJECTIVE.md                       # the goal handed to vibeserve-orchestrate
├── README.md                          # this file
├── benchmark/
│   ├── benchmark.py                   # /v1/completions driver, single-batch
│   └── README.md
├── accuracy_checker/
│   ├── checker.py                     # quality gate — prevents echo-input bypass
│   └── README.md
└── reference/
    ├── README.md                      # how to mount the target model
    └── meta.json                      # pinned model ids
```

## Request envelope

The benchmark drives an OpenAI-compatible `/v1/completions` endpoint. Every request body is:

```json
{
  "prompt": "<chat-templated instruction containing the buggy code>",
  "prediction": {"type": "content", "content": "<the buggy original code, verbatim>"},
  "max_tokens": 512,
  "temperature": 0,
  "stream": true,
  "prompt_is_preformatted": true
}
```

The `prediction` field is the OpenAI predicted-outputs format. Servers that don't implement it (vLLM, SGLang today) just ignore it; a server that does implement it is the configuration this benchmark scores.

## How to run

Launch the server, then:

```bash
uv run python inputs/qwen3-32b-code-edit/benchmark/benchmark.py \
    --url http://localhost:8000 --model qwen3-32b \
    --num-samples 100 \
    --output-json /tmp/code_edit_baseline.json
```

The bench prints the headline `Primary metric: median_tok_per_sec = ...` line and writes per-sample alignment + quality stats to the output JSON.

## Accuracy gate

`accuracy_checker/checker.py` is the anti-reward-hacking gate. It enforces:

- Output must be **closer to the gold solution than to the buggy input** (a server that just echoes the prediction back fails this).
- Output must not equal the buggy input verbatim (degenerate "no edit" bypass).

These gates are intentionally cheap so they don't dominate the perf budget — the harder integration test is left to `bench/benchmark.py`'s per-sample diff stats.
