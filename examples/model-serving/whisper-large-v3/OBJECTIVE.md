# Objective — whisper-large-v3 offline ASR server

Maximize **offline transcription throughput** (audio-seconds transcribed per
wall-second, and requests/second) under concurrent batch load, while keeping
accuracy within the accuracy checker's tolerance. Expose an OpenAI-compatible
`/v1/audio/transcriptions` endpoint that accepts an uploaded audio file and
returns the transcript.

This is the *offline batch* counterpart to `moonshine-streaming` (which targets
streaming TTFT): here requests are complete clips submitted concurrently, and
the win is amortizing the encoder and batching the decoder across requests
rather than driving down per-chunk latency.

## Notes

- **Encoder-decoder ASR.** whisper-large-v3 is a 32-layer conv+transformer
  audio encoder feeding a 32-layer text decoder that cross-attends to the
  encoder output at every step. The forced decoder prompt is
  `<|startoftranscript|><|en|><|transcribe|><|notimestamps|>`; greedy decode to
  `<|endoftext|>` (or 448 target positions).

- Implement the encoder, decoder, cross-attention, and the log-mel front end in
  your own serving code. Use `transformers` only as a utility for config /
  tokenizer / feature extractor / weight loading — not for `generate()`.

- **Levers for offline throughput (roughly in impact order):**
  - **Cross-attention K/V caching.** The decoder's cross-attention keys/values
    depend only on the (fixed) encoder output. Project them once per request at
    prefill and reuse for the whole decode loop instead of reprojecting every
    step — this is a large constant factor on the per-step cost.
  - **Continuous batching of the decode loop.** Requests finish at different
    step counts; batch the active decoder steps across requests each tick so the
    32-layer decoder runs once per tick for the whole batch, not once per
    request. Pad/pack the self-attention KV cache accordingly.
  - **Batch the encoder.** The 30 s log-mel window is a fixed shape, so the
    encoder is trivially batchable — run one encoder forward per arrival batch
    rather than one per request.
  - **CUDA graphs / paged KV.** Capture the fixed-shape decode step and page the
    self-attention KV cache to cut launch overhead and fragmentation at higher
    concurrency.
  - **Self-attention only in the cache.** Whisper has no RoPE (learned absolute
    positions added at embed time), so the self-attention cache is a plain
    growing KV; only the decoder self-attention writes it — cross-attention
    reads the fixed context pool (see above).

- Whisper truncates/pads audio to a fixed 30 s mel window; clips beyond 30 s are
  out of scope for this target (the test set is short LibriSpeech utterances).

- The benchmark harness drives `/v1/audio/transcriptions` with `--concurrency`
  concurrent clients over the test-audio pool. Throughput (audio-s/wall-s and
  req/s) is the primary metric; end-to-end latency (mean / p50 / p95 / p99) is
  secondary.
