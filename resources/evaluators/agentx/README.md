# AgentX evaluator

`agentx-v1` runs the pinned `agentx-harness==1.0.7` in a Python 3.12 uv script
environment, independent of candidate dependencies. The adjacent script lock
pins transitive dependencies; `uv`, Python download/package access and public
Hugging Face corpus access are required on first setup. Later runs reuse caches.
The manifest supplies task policy; this package owns replay and result translation.

From a project with a running Qwen server:

```bash
uv run --locked --script /path/to/evaluator/agentx_eval.py \
  --model Qwen/Qwen3.5-9B --tokenizer Qwen/Qwen3.5-9B \
  --tokenizer-revision c202236235762e1c871ad0ccb60c8ee5ba337b9a \
  --max-context-length 262144 --concurrency 4 \
  --url http://127.0.0.1:8000 --mode full \
  --artifact-dir /path/to/empty-results --vs-output /path/to/result.jsonl
```

Full mode uses the `agentx` scenario, date-pinned June 21 corpus, a 15-minute
measurement, fixed seed and explicit trajectory/warmup settings. It exports
`output_tokens_per_s`, `ttft_p50_ms`, `ttft_p90_ms`, `tpot_p50_ms`,
`tpot_p90_ms`, and diagnostic `p90_latency_ms` using VibeSys result
protocol 2. Units are checked against AIPerf's JSON export. Any measured error
fails this task, even when upstream's submission-validity rules allow it.
AIPerf can omit an unused error counter (or serialize it as `null`). This
means zero only when `error_summary` explicitly contains an empty list.
A missing error summary, nonempty error summary, or nonzero counter is rejected.
Raw artifacts are retained only when `--artifact-dir` is supplied. It must
be empty or absent, preventing stale exports from being mistaken for new work.

`--mode smoke --concurrency 1` shortens replay to two eligible entries and
60 seconds, explicitly setting `--unsafe-override`. It returns exit status
zero only when real requests and result parsing succeed, prints unscored
metrics, and writes an error record instead of a result if `--vs-output` is
provided. It cannot produce an accepted VibeSys measurement.

The seed in the Qwen example does not yet implement chat or long-context
serving; use its declared vLLM comparison to check the harness independently.
