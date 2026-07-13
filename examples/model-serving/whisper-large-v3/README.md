# whisper-large-v3 — offline ASR serving target

A VibeServe target for **offline (batch) automatic speech recognition** with
`openai/whisper-large-v3`. The candidate synthesizes a serving system that
exposes an OpenAI-compatible `/v1/audio/transcriptions` endpoint and maximizes
transcription throughput under concurrent load while matching the HuggingFace
reference's transcripts.

This is the offline-batch counterpart to `moonshine-streaming` (streaming TTFT):
the algorithmic wins here are cross-attention K/V caching, continuous batching of
the decoder across requests, and batching the fixed-shape encoder — not per-chunk
incremental encoding. See [`OBJECTIVE.md`](OBJECTIVE.md).

## Layout

```
whisper-large-v3/
├── vibeserve.input.toml   # manifest: domain, accuracy + benchmark commands, headline metric
├── OBJECTIVE.md           # what to optimize + the algorithmic levers
├── reference/             # HF WhisperForConditionalGeneration reference (correctness ground truth)
│   ├── meta.json          # model id + pinned revision
│   ├── config.json        # whisper-large-v3 config
│   └── reference.py       # reference_transcribe() used by the checker
├── accuracy_checker/      # checker.py — reference-vs-candidate word-overlap gate
├── benchmark/             # benchmark.py — offline throughput (requests_per_second)
└── test_audio/            # LibriSpeech test-clean clips + manifest.json
```

## Candidate contract

The Implementer's `main.py` must expose:

```python
class VibeServeModel:
    @classmethod
    def from_pretrained(cls, model_dir, device, dtype) -> "VibeServeModel": ...
    def transcribe(self, audio: np.ndarray, sampling_rate: int = 16000) -> str: ...
```

and serve `/v1/audio/transcriptions` for the benchmark.

## Run it

```bash
./vs \
  --input examples/model-serving/whisper-large-v3 \
  --exp-name whisper-offline \
  --docker \
  --agent-backend cli --cli-provider codex \
  --max-rounds 4 \
  --modality speech_to_text
```

## Test set

Four LibriSpeech `test-clean` utterances (4.8–12.5 s) with ground-truth
transcripts in `test_audio/manifest.json`. The accuracy gate compares the
candidate against the HF reference (not the ground truth directly), so it is
robust to whisper's own residual errors — a candidate passes by *matching the
reference implementation*, which is the correctness contract VibeServe checks.
