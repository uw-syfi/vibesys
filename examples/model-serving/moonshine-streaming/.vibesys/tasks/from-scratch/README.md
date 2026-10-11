moonshine-streaming-medium input bundle.

Use:

```bash
vibesys --runs-dir /work/vibesys-runs --local \
  --input examples/model-serving/moonshine-streaming --task from-scratch
```

Streaming ASR: clients send 16 kHz mono PCM audio in chunks; server emits partial transcripts per chunk and a final transcript on finalize.  The benchmark drives concurrent clients each pushing audio every N seconds in real-time pacing and measures TTFT, TPOT, and audio-seconds-per-wall-second.

Audio samples are 16 kHz mono.  See `test_audio/manifest.json` for ground truth transcripts.
