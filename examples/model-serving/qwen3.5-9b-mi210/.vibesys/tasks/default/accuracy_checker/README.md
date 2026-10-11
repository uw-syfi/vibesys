# Accuracy checker: Qwen3.5-9B

Gates a serving engine against HF transformers greedy output on a fixed prompt
set. Exit status 0 = PASS, 1 = FAIL. Run from the bundle root.

```bash
# any running server (reference, candidate, or vLLM); --target http is the default
uv run python accuracy_checker/checker.py --base-url http://127.0.0.1:8000
# in-process reference engine
uv run python accuracy_checker/checker.py --target inproc
# add --json-out result.json for the full per-case report
```

The HTTP target needs `/v1/completions` with `ignore_eos`, `return_token_ids`,
and `echo` + `logprobs` + `return_tokens_as_token_ids` (vLLM-compatible), and
`usage.prompt_tokens_details.cached_tokens` on non-streamed responses. It also
runs the cache-resume check below and checks that a streamed response yields
the same token ids and a correct usage chunk.

## Golden data

`golden.json` (checked in) holds, for 12 prompts (`prompts.py`: raw and
chat-templated, code, arithmetic, JSON, and two long ledger prompts of ~1.2k
and ~4k tokens): frozen prompt token ids, 64 HF greedy tokens with EOS ignored,
and HF's teacher-forced logprob, top-1 id, and top-1 minus top-2 margin at each
of those 64 positions. Regenerate only if the prompt set changes:

```bash
uv run python -m accuracy_checker.make_golden --model Qwen/Qwen3.5-9B
```

Source: HF `Qwen3_5ForCausalLM`, bf16, SDPA attention, fla 0.5.2 GDN kernels,
transformers 5.17.0, torch 2.9.1+rocm6.4, MI210 (`meta` in the file).

## Gate policy

bf16 with a different kernel or reduction order does not reproduce HF's greedy
tokens bit-for-bit: a near-tie (top-1 and top-2 within bf16 noise) can flip,
after which the continuations legitimately differ. So the gate never requires
full-length token equality. It requires that the candidate compute the same
distribution, measured two ways:

1. Teacher-forced (primary). The candidate scores prompt + golden
   continuation. Per position, compare its logprob of the golden token with
   HF's, and its argmax with HF's top-1. No divergence cascade, so every one of
   the 768 positions is informative.
2. Free-running greedy. The candidate generates 64 tokens per prompt. Where it
   first departs from golden, the contexts were identical up to that point, so
   HF's margin at that position says whether the departure is a near-tie.

| Check | Threshold |
|:--|:--|
| Free-run: every first divergence is at a near-tie (HF margin < 0.5 nats) | 0 non-tie divergences |
| Teacher-forced: argmax differs from HF top-1 where HF margin >= 0.5 nats | 0 flips |
| Teacher-forced mean \|Δlogprob\| of the golden token | <= 0.05 |
| Teacher-forced p99 \|Δlogprob\| | <= 0.25 |
| Free-run mean matched-prefix fraction | >= 0.5 |

Thresholds are fixed in `thresholds.Thresholds` and were set from the
calibration below, before any optimized engine existed. Do not retune them to
admit a candidate.

## Cache-resume check

The checks above never hit a prefix cache: servers compute `echo` + `logprobs`
requests without it, and each free-run prompt is sent once. On this workload
most tokens are served by resuming a session from the previous round's cached
KV and GDN state, so a resume bug (stale or wrong GDN state, an off-by-one
resume position, KV reused without the matching state) would pass them. The
HTTP target therefore also runs `resume.py` (before the checks above, so it
resumes from the end of a finished request):

- Each case's 64 golden tokens are generated as 4 chained rounds of 16. Round
  k's prompt is the case prompt plus the golden tokens before round k. When
  round k-1 matched golden, that is exactly round k-1's prompt plus its
  output, as in a session replay, and the resume position is not aligned to
  a 64-token GDN chunk. Anchoring on golden rather than on the server's own
  output keeps later rounds checkable after a near-tie divergence.
- Each round is judged by the free-run policy: its first divergence from
  golden must be at a near-tie (HF margin < 0.5 nats), and the mean matched
  fraction over all rounds must be >= 0.5. A non-tie divergence fails the
  gate whether or not the round reported a cache hit; the report lists the
  ones with `cached_tokens > 0` separately
  (`cache_hit_non_tie_divergences`), since those point at the resume path.
- Every round must report `usage.prompt_tokens_details.cached_tokens` with
  `0 <= cached_tokens <= prompt tokens`.

A server without prefix caching reports `cached_tokens: 0`, recomputes every
round, and passes on correctness alone; the report then notes that the resume
path was not exercised. The report also gives, per round, the reported
`cached_tokens` against `resumable_tokens` (the tokens of that prompt whose
state the previous round computed), which shows whether a caching server
actually resumed. A hit larger than that is not an error: another request
(for example an earlier gate run) may have cached a longer prefix.

A hit smaller than `resumable_tokens` is not an error either: caches are
block-granular (vLLM's hybrid attention/GDN block is 528 tokens on this model,
so only the two ledger prompts hit), and an engine that parks GDN state only
at request ends cannot resume in the middle of a previous output.

The check reuses the base thresholds and golden data unchanged. Measured on
one MI210, 2026-09-23 (second run of each against the same warm server gave
the same verdicts):

| Server | Cache-hit rounds | cached / resumable tokens | Non-tie div. | Mean round prefix frac. | Gate time | Result |
|:--|--:|--:|--:|--:|--:|:--|
| Reference engine (no prefix cache) | 0/48 | 0 / 18472 | 0 | 0.961 | 102 s | PASS |
| Tuned vLLM (`benchmark/vllm_baseline.sh`) | 6/48 | 15840 / 18473 | 0 | 0.961 | 31 s | PASS |
| Campaign engine (prefix cache + MTP) | 34/48 | 18285 / 18452 | 0 | 0.939 | 17 s | PASS |

Gate time is the whole checker run (base, resume, and stream checks).

## Calibration evidence

Measured on MI210, 2026-09-23, against the checked-in golden (HF with fla):

| Candidate | Identical | Non-tie div. | Flips | mean \|Δlp\| | p99 \|Δlp\| | max \|Δlp\| | Prefix frac. | Result |
|:--|--:|--:|--:|--:|--:|--:|--:|:--|
| Reference engine, in-process | 8/12 | 0 | 0 | 0.0055 | 0.079 | 0.111 | 0.855 | PASS |
| Reference engine, over HTTP (+ stream check ok) | 8/12 | 0 | 0 | 0.0055 | 0.079 | 0.111 | 0.855 | PASS |
| HF with torch GDN fallback (no fla) | 8/12 | 0 | 0 | 0.0055 | 0.079 | 0.111 | 0.855 | PASS |
| Fault: layer 0 GDN decay gate off | 1/12 | 6 | 78 | 0.507 | 8.83 | 11.3 | 0.30 | FAIL (5/5 checks) |
| Fault: layer 16 GDN decay gate off | 5/12 | 2 | 7 | 0.038 | 0.569 | 2.32 | 0.647 | FAIL (3/5 checks) |
| Fault: layer 15 attention output gate constant 0.5 | 1/12 | 7 | 38 | 0.149 | 2.03 | 6.91 | ~0.3 | FAIL (5/5 checks) |

Notes:
- The reference engine is bit-identical to HF's torch fallback path: scored
  against a golden produced with `--no-fla`, it gives 12/12 identical and
  Δlogprob 0.0 everywhere. So the whole Δ above is HF-fla vs HF-torch kernel
  noise, and it is the noise floor a correct engine sees. The four reference
  divergences are at HF margins 0.0, 0.125, 0.125, and 0.25 nats.
- Margin to thresholds for the reference: mean Δlp 9x under, p99 3x under,
  max Δlp 0.11 vs the 0.5-nat near-tie cut, 0 flips.
- The subtle fault (decay off in one middle layer) stays under the mean Δlp
  threshold; the flip, near-tie, and p99 checks catch it. The mean is the
  least sensitive check; do not rely on it alone.
- Faults are injected by `--fault` (in-process target only), e.g.
  `--fault gdn-decay-off:16`, `--fault attn-gate-off:15`.

## Same-numbers vs different-numbers changes

"Same-numbers": the change should leave every logit bit-identical to the
reference for the same request (the gate passes with the reference's own
figures). "Different-numbers": the change legitimately alters rounding; it must
still pass the gate, and the reviewer should expect Δlogprob to move.

| Change | Class |
|:--|:--|
| HTTP/server plumbing, detokenization, streaming, CPU/GPU overlap, async scheduling | same-numbers |
| Continuous batching / paged KV where each sequence's math is unchanged (per-row GEMM results can still vary with batch size on rocBLAS/hipBLASLt; treat as different-numbers if they do) | same-numbers in intent, verify |
| HIP graph capture of the identical kernel sequence | same-numbers |
| Prefix caching of attention KV and GDN state snapshots | same-numbers for the cached part; chunk boundaries change GDN reduction order |
| Chunked prefill (changes GDN chunk boundaries and attention tiling) | different-numbers |
| fla / Triton GDN kernels, causal-conv1d kernels, flash attention, fused norms/activations/rope | different-numbers |
| Batched decode GEMMs (M > 1) | different-numbers |
| GDN recurrent state or KV cache in a lower precision than the reference (state fp32, KV bf16) | different-numbers, likely fails |
| Weight or activation quantization (e.g. int8/fp8 emulation; MI210 has no FP8) | different-numbers, likely fails |
