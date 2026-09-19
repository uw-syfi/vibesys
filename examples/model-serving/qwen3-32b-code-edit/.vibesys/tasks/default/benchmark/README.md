# benchmark/benchmark.py

Single-batch code-edit latency benchmark on `m-a-p/CodeEditorBench` code-debug rows. Drives an OpenAI-compatible `/v1/completions` server with concurrency = 1.

## What it sends

Each request body:

```json
{
  "prompt": "<chat-templated instruction containing the buggy ```python3 ...``` block>",
  "prediction": {"type": "content", "content": "<the buggy original code, verbatim>"},
  "max_tokens": 512,
  "temperature": 0,
  "stream": true,
  "prompt_is_preformatted": true
}
```

`prediction.content` is OpenAI's [predicted-outputs](https://developers.openai.com/api/docs/guides/predicted-outputs) field. vLLM/SGLang ignore it today (the request still parses and runs as a normal completion); a server that consumes the field is the configuration this benchmark scores.

## What it captures (per sample)

For each successful response, in addition to the standard latency / TTFT / TPOT:

- **Token-level alignment vs the prediction**: for each accepted output, tokenize both the buggy input and the model's output with the Qwen3 tokenizer, run a `difflib.SequenceMatcher` on the token-id sequences, and record:
  - `num_matched_tokens`, `num_diverged_tokens`
  - `matched_run_lengths` — list of token-lengths of every "equal" block in the output
  - `longest_matched_run`
- **Quality**: `ratio_to_gold`, `ratio_to_input`, `equals_input_verbatim`. The gate `ratio_to_gold > ratio_to_input` distinguishes a real fix from "echo the buggy input back."

These fields are what an analytical predicted-outputs headroom estimate would consume — the bench just emits them, the math lives downstream.

## Token count canonicalisation

`output_tokens` is computed by re-tokenising the concatenated server response — independent of how many tokens the server batches per SSE chunk. vLLM/SGLang flush all accepted spec tokens in one chunk, which would otherwise massively undercount tok/s. `num_chunks` is reported separately so you can also see effective spec accept length.

## Headline metric

```
Primary metric: median_tok_per_sec = ...
```

This is what `perf_metric` records and what the orchestrator's plateau detector compares across rounds.

## Running

```bash
uv run python benchmark.py \
    --url http://localhost:8000 \
    --num-samples 50 --warmup 3 \
    --max-tokens 512 \
    --output-json /tmp/code_edit.json
```

Default `--languages python3`. To include cpp/java rows: `--languages python3,cpp,java`. Token-level alignment math is cleanest on Python (BPE chunking interacts well with the dataset's whitespace), so default is python3 only.
