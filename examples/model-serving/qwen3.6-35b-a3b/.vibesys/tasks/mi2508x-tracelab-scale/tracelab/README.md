# TraceLab concurrency-sweep adapter

This task measures how the engine scales with load on real TraceLab coding-agent
traffic. `replay.py` replays one trace at rising session concurrency, one
fixed-time window per step, and scores three objectives: the peak throughput over the steps
that meet a latency cap, and TTFT p50 / TPOT p50 at the fixed level 4. The image's trusted `session_runner` is
Request Factory revision `89dce4a64ae12e7084fbe464ddda882fa0a4c482` plus
`../request-factory/windowed.patch` (two commits adding `--warmup-secs` /
`--duration-secs`, see `../Dockerfile`).

## Source and licence

The trace derives from TraceLab v0.0.2, `syfi_coding_trace.jsonl.gz`
(sha256 `11ce51ec0a25e3d1d95b025bca2f7d1647e47571eb7cc968acd5fc64d4b4fb65`),
from [TraceLab](https://github.com/uw-syfi/TraceLab), licensed
[CC BY 4.0](https://github.com/uw-syfi/TraceLab/blob/main/LICENSE-DATASET.md).
Attribution is to the TraceLab authors. The data was sampled and re-timed as
described below; it is not the original dataset.

## How the trace was built

`build_trace.py` (a provenance copy; the task does not run it) built `trace.csv`
deterministically (seed 0). The trace is drawn from a subset of TraceLab users; the
rest are held out for evaluation. `build_manifest.json` and `trace.manifest.json` record
the exact inputs, parameters and counts. In outline:

1. Verify the source sha256, parse the JSONL as data, and order each session's
   rounds by round index. `input_len`, `prefix_len`, `output_len` and tool wait
   come from TraceLab's recorded token counts and tool latencies (tool waits are
   capped at 60 s).
2. Drop sessions whose peak context exceeds 262,144 tokens (367 of 8,058; 7,691
   remain eligible).
3. Sample 2,000 sessions with replacement from the eligible sessions of that subset. Each copy
   starts part-way through its session, at a uniformly random point between 25%
   and 75% of its rounds. The rounds before the start are folded into the first
   round's prefix, so every sampled session begins with a long, cold first
   prompt (1,906 first rounds carry folded prefix, 102.9 M tokens in all).
4. Emit canonical `session-execution-v2` rows with Request Factory's `tracegen`
   (`--session-order source`).

Result: 2,000 sessions, 42,439 rounds, 24.0 M output tokens, peak context 260,791
tokens (p50 65,359, p90 169,258), median first prompt about 46k tokens. The
canonical CSV (sha256 `a2546a138e9fcb48e583faeb696b2fe1e748b6a5524b7c1bcae1183f859d66bc`,
checked by `replay.py`) already holds the request ids and arrival times the runner
needs. The 17 MB generation plan is not included.

## How the sweep works

* Steps: concurrency 4, 8, 16, 32, 64, 128, 256 sessions. Each step is one
  `session_runner` process: `--arrival-mode saturated --max-concurrency N
  --warmup-secs 60 --duration-secs 180 --max-model-len 262144 --temperature 0
  --request-log false --timeline false`, backend `openai` on `/v1/completions`.
  Replay is in trace order, so every step starts from the same sessions. Warmup, window and
  cooldown are the constants `WARMUP_SECS`, `DURATION_SECS` and `COOLDOWN_SECS` in `replay.py`.
* Each runner first runs its prefix-cache preflight (which needs genuine cache-hit
  accounting from the server), then measures the window
  [t0+60 s, t0+240 s), then cancels everything in flight. Cancelled requests are
  not failures; real failures are.
* Throughput counts output tokens that arrive inside the window, divided by 180 s;
  TTFT and TPOT are windowed. A request still waiting for its first token at window
  end contributes a censored lower bound to TTFT, so starving long prompts cannot
  improve it.
* Fresh text per step: every step tokenizes a corpus built with a new random nonce,
  since all steps replay the same prefixes and would otherwise hit the server's
  prefix cache with content from earlier steps. The token pool is 8,000,000 tokens
  (about 30 times the longest prompt, with distinct per-session start offsets); the
  reasoning is in `replay.py`.
* 15 s of cooldown between steps lets the server drain cancelled work.
* Latency cap: a step passes if TPOT p50 <= 100 ms and TTFT p50 <= 10,000 ms.
* Climb: ascending; continue while a step passes the cap and its tok/s is at least 1.03 times
  the best tok/s of the earlier passing steps. The first step that fails either ends it.
* Fixed level: 4 always runs, and the climb starts there.
* Refinement: with p the best passing step, if both p and 2p ran, one more step runs at
  round(p x sqrt(2)): 6, 11, 23, 45, 91 or 181 for p = 4, 8, 16, 32, 64, 128. It is under the
  same cap and is a peak candidate.
* Peak: the throughput objective is the maximum tok/s over all passing steps (refinement
  included). `peak_concurrency` is the lowest-concurrency passing step whose tok/s is at least
  97% of that maximum. If no step passes the cap, the objective is the tok/s of the lowest
  step run, `peak_concurrency` is that step, and `latency_cap_met` is 0 (otherwise 1).
* Objectives (exactly three): `output_tokens_per_s` (the peak) and `ttft_p50_ms_c4`,
  `tpot_p50_ms_c4`, from the step at 4. Latency under heavy load is held by the cap.
  Promotion is decided on these three; tail percentiles are reported but not scored at this
  tier. `objectives.toml` carries the per-objective `relative_noise` floors.
* Reported, not objectives (declared `required: false` in the hello): `peak_concurrency`,
  `latency_cap_met`, `tok_s_at_c<N>` for every step that ran, and `ttft_p90_ms_c4`,
  `tpot_p90_ms_c4`. A per-step table is written to
  stderr.
* Validation: any failed request (in or before the window), any output-length
  mismatch, any request whose server streamed no token ids, an empty window, a
  nonzero runner exit, window parameters that differ from the request, or a missing or
  non-finite metric fails the whole measurement with a protocol error.
* Time: the overall deadline is 3,300 s of the 3,600 s benchmark timeout. The step at 4 runs
  first; the deadline cuts only the climb above it and the refinement. If 4 cannot finish,
  the result is a protocol error. Each runner process is
  limited to window length plus 300 s.

## Sweep mode (development, unscored)

`--sweep` runs a few concurrency levels with shorter windows against the running server, to
see how an engine scales without paying for a scored run. It is a command in the task image
(`--image harness`), run beside the candidate's server (`VIBESYS_SERVICE_URL`):

```
vibesys-cli submit command <sha> --image harness -- \
  python3 -I /task/tracelab/replay.py --request-factory-engine /usr/local/bin/session_runner \
  --sweep -- --concurrency 8,24 --window-s 90
```

The first `--` ends vibesys-cli's own options; the second ends the adapter's. Settings are the
arguments after the second `--`, parsed by a separate narrow parser that knows only
`--concurrency` (comma-separated, 1-8 unique integers in 1..512, run in order; default
`4,16,64`), `--warmup-s` (default 30), `--window-s` (default 60) and `--cooldown-s` (default 10),
all integers. Nothing after `--` reaches the task's own parser, so it cannot set or override
`--request-factory-engine`, `--vs-output` or `--test-*`; unknown arguments are an error and
argparse prefix abbreviations are off on both parsers. This is not a trust boundary: sweep runs
are never scored, and `submit measure` runs the fixed `[benchmark]` command with no agent
arguments. It keeps sweep numbers meaning what they say: the canonical arguments describe
everything a sweep run lets an agent vary. One function validates every path and returns the
canonical settings; the canonical argument form has a fixed key order with defaults filled,
e.g. `--concurrency 8,24 --warmup-s 30 --window-s 90 --cooldown-s 10`.

`--check-args` (with `--sweep`) only validates, using nothing but the standard library and no
server, network, tokenizer, `session_runner` or GPU, and needs neither `--request-factory-engine`
nor `--vs-output`:

```
python3 -I replay.py --sweep --check-args -- --concurrency 8,24
{"ok": true, "args": ["--concurrency", "8,24", ...], "settings": {...}}      # exit 0
{"ok": false, "error": "..."}                                                # exit 2, also on stderr
```

The planned time, the sum over steps of warmup + window + cooldown + a 90 s setup allowance,
may not exceed 1,500 s. The real run validates first, echoes the canonical arguments on stderr
and records them as `args` in `sweep.json`. Each step goes through `run_step` and `step_metrics`
like a scored step (fresh nonce corpus, windowed measurement, validation), with the sweep
windows. stdout gets a table (tok/s, TTFT/TPOT p50/p90, requests, TTFT-censored requests,
latency cap) and a notice that the numbers are unscored and not comparable with
`submit measure`; if `VIBESYS_ARTIFACTS_DIR` is set the same data is written to `sweep.json`
there. A step that fails validation is reported and the remaining steps run; the exit status is
nonzero if any step failed. Sweep mode takes no `--vs-output` and writes no result. The
benchmark argv passes no `--sweep`.

`--sweep` needs `session_runner`, so it runs only in the task image.

The adapter reads the exact-revision Qwen tokenizer from the operator's read-only
Hugging Face cache (offline). Run `python3 -I test_replay.py` for the unit tests of
the sweep logic.
