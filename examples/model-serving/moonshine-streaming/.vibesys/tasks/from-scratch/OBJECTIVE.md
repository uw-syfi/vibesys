# Objective — moonshine-streaming-medium inference server

Minimize **TTFT** (time from a client pushing an audio chunk to the server returning the partial transcript for that chunk) under concurrent streaming ASR load, while keeping accuracy within the accuracy checker's tolerance.  Expose a WebSocket streaming endpoint that accepts 16 kHz mono int16 PCM chunks and emits partial transcripts per chunk plus a final transcript on finalize, alongside an OpenAI-compatible `/v1/audio/transcriptions` endpoint for offline transcription.  The encoder should be CUDA-graphed (capture/replay the per-chunk incremental encoder forward at fixed shape buckets) to minimize per-chunk launch overhead and drive TTFT down.

## Notes

- Encoder-decoder ASR with a streaming-friendly encoder: per-layer
  asymmetric sliding-window attention (left context + bounded right
  look-ahead), so the encoder can be advanced incrementally as new
  audio arrives without re-running on the full prefix every chunk.
- Implement encoder, decoder, conv frontend (cmvn + asinh + linear + 2
  causal stride-2 convs), interleaved RoPE, and layer norms in your
  own code.  Use `transformers` only as a utility for config /
  tokenizer / weight loading.
- **Incremental encoding** is the headline algorithmic win for low
  TTFT.  Without it, per-chunk latency grows linearly with session
  length because the encoder re-runs over the entire accumulated audio
  every chunk.  What to cache, per session:
    - **Per-layer K/V cache** (paged or dense), one entry per encoder
      output frame.  On each new chunk, layer L appends exactly `n_new`
      fresh slots covering positions `[T - lookahead[L], T+n_new -
      lookahead[L])` where `lookahead[L]` is the cumulative right-context
      from layers `<L` (sum of their `right_window` values).  No rewinds
      needed if you only ever write *stable* slots — i.e. positions
      whose right-context has fully arrived in audio.
    - **Per-layer input carry** of length `right[L]`.  Layer L's queries
      this chunk start `right[L]` frames earlier than its K/V writes
      (queries see a wider window than the K/V they project from), and
      that prefix comes from the *previous* layer's output emitted in
      the *previous* chunk.  Save the last `right[L]` frames of each
      layer's input across chunks.  For the real model the per-layer
      `right` values are mostly 0 with `[4,4,0,…,0,4,4]` at the
      boundaries, so most carries are empty.
    - **Conv-frontend raw-sample carry**.  The two stride-2 causal convs
      have a non-trivial receptive field at the raw-audio level.  Keep
      `recep_field - total_stride` raw samples between chunks so the
      first new conv frame's window is fully populated; on the first
      chunk, zero-pad to that length so frame counts align with the
      non-streaming reference.
    - **Cross-attn cache** is independent of incremental encoding but
      pays the same TTFT dividend: project the encoder hidden states
      through every decoder layer's `cross_k`/`cross_v` once at request
      start and reuse across the entire decode loop, instead of
      reprojecting every step.
- Continuous batching across sessions: streaming clients arrive and
  finalize at independent times.  The scheduler should batch encoder +
  decoder steps across active sessions per tick — but the scheduler
  tick itself adds to TTFT, so keep it tight.
- Benchmark harness drives the server with `--concurrency` concurrent
  streaming clients pushing audio at **real-time pacing**.  TTFT is
  the primary metric (mean / p50 / p95 / p99); audio-seconds-per-wall-second
  and TPOT are secondary.  The harness also has an offline mode for
  vLLM-comparable HTTP sweeps.
