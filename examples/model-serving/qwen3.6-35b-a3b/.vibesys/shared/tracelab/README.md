# TraceLab recorded-sample adapter

The two `*-tracelab` tasks reuse the pinned Request Factory evaluator's trusted
`session_runner`. They replay all 48 recorded sessions / 303 rounds in
[Request Factory's TraceLab sample](https://github.com/uw-syfi/request-factory/blob/89dce4a64ae12e7084fbe464ddda882fa0a4c482/examples/multi_session_large.csv).
`recorded_sessions.csv` preserves the source rows with LF line endings; `replay.py` verifies its SHA256
and adds the request IDs and zero arrival times needed by the v2 input schema.
Saturated replay makes the zero arrival times explicit and unused. Original
round ordering, lengths, prefixes and tool waits remain unchanged.

This is a small recorded sample from [TraceLab](https://github.com/uw-syfi/TraceLab),
not its entire public dataset or a statistical expansion. A synthetic coding
text token pool supplies payload bytes; private developer prompt text is not
included. A fresh per-run nonce prevents a prior run from warming the same
text pool. Eight concurrent session slots include tool waits. The runner's
prefix-cache preflight requires genuine cache-hit accounting from the server.
There is no separate warmup or unscored smoke mode in this adapter.

The original TraceLab dataset is [CC BY 4.0](https://github.com/uw-syfi/TraceLab/blob/main/LICENSE-DATASET.md);
attribution is to the TraceLab authors. The source rows are unchanged.

The adapter downloads the exact-revision Qwen tokenizer and creates the token
pool before invoking the runner. Native measured timing excludes setup and
compilation; first-run kernels inside replay remain included. Results use
VibeSys protocol 2 and the five throughput/TTFT/TPOT axes. Missing, nonfinite or
negative metrics, failed/incomplete rounds, and wrong output totals are errors.
The subprocess deadline is 3000 seconds, inside the task's 3600-second budget.
Full model execution and live throughput have not been validated on AMD.
