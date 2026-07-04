moonshine-streaming-medium input bundle.

Use:
- `--ref inputs/moonshine-streaming/reference`
- `--acc-checker inputs/moonshine-streaming/accuracy_checker`
- `--bench inputs/moonshine-streaming/benchmark`

Each folder contains scripts plus a short README.

Streaming ASR: clients send 16 kHz mono PCM audio in chunks; server emits partial transcripts per chunk and a final transcript on finalize.  The benchmark drives concurrent clients each pushing audio every N seconds in real-time pacing and measures TTFT, TPOT, and audio-seconds-per-wall-second.

Audio samples are 16 kHz mono.  See `test_audio/manifest.json` for ground truth transcripts.
