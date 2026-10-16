# Correctness checker

Run `uv run --offline --no-dev python .vibesys/tasks/shared-prefix/accuracy_checker/checker.py --help`
from the example root for CLI options. `--model-path` or `MLX_MODEL_PATH` must
identify the pinned cached snapshot. `--artifact-dir` must be a new directory;
the default uses a unique evaluation ID under `artifacts/`.

The checker owns a fresh stock/candidate server, validates warmup, and submits
four concurrent streaming questions about randomized sentinel facts. A second
document changes those facts and question order to detect stale/swapped answers.
Every response must have the correct answer, valid incremental OpenAI-style SSE,
actual token usage, a completion termination, and `[DONE]`. Plain JSON responses
to a streaming request fail. Any warmup, request, or cleanup failure invalidates
the check. Artifacts preserve the exact experiment and error diagnostics.

`--help` does not load weights or require Metal. The real checker does require
Metal and is a manual validation step for this onboarding phase.

The current stock candidate uses `--prompt-cache-size 1`, a native retained-cache
cap for the 16 GB Mac. Both documents run in the same process. The cap bounds
completed-request entries, not active decoding or total unified memory; a live
pass is still required. Preserve the default-10 failure artifacts separately.

Correctness monitoring checks the combined `server.log` for fatal worker errors
and aborts unfinished streams without waiting for the HTTP read deadline.
Benchmark timing does not run this monitor. Failures retain exception type and
repr, phase, seed/question, elapsed time, stream termination state, process
status before cleanup, cleanup status, and the relevant stdout/stderr tail.

A pass exits zero and prints `Correctness passed: 8 scored responses.` followed
by the artifact path. Preserve the entire directory, including invocation,
model/workload records, request traces, server log/resource/status files, and
`evaluation.json`. A failed run is evidence only and cannot establish a baseline.
