# whisper-large-v3 — offline ASR serving target

A VibeServe target for **offline (batch) automatic speech recognition** with
`openai/whisper-large-v3`. The candidate synthesizes a serving system that
exposes an OpenAI-compatible `/v1/audio/transcriptions` endpoint and maximizes
transcription throughput under concurrent load while matching the HuggingFace
reference's transcripts.

This is the offline-batch counterpart to `moonshine-streaming` (streaming TTFT):
the algorithmic wins here are cross-attention K/V caching, continuous batching of
the decoder across requests, and batching the fixed-shape encoder — not per-chunk
incremental encoding. See [`OBJECTIVE.md`](.vibesys/tasks/default/OBJECTIVE.md).

## Layout

```
whisper-large-v3/
└── .vibesys/tasks/default/  # trusted task files; read-only for the agent
    ├── vibesys.input.toml   # manifest: domain, accuracy + benchmark commands, headline metric
    ├── OBJECTIVE.md         # what to optimize + the algorithmic levers
    ├── reference/           # HF WhisperForConditionalGeneration reference (correctness ground truth)
    │   ├── meta.json        # model id + pinned revision
    │   ├── config.json      # whisper-large-v3 config
    │   └── reference.py     # reference_transcribe() used by the checker
    ├── accuracy_checker/    # checker.py: reference-vs-candidate word-overlap gate
    ├── benchmark/           # checked-in Request Factory workload trace
    └── test_audio/          # LibriSpeech test-clean clips + manifest.json
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

The benchmark is executed directly by the pinned
`vibesys-evaluator-request-factory` engine. Its checked-in 64-request trace
cycles over the four WAV fixtures under saturated load at concurrency eight.
VibeSys reads `request_throughput_per_s` from the Request Factory summary. No
warmup requests are excluded, so cold-start work is part of the score. Request
Factory measures from first submission to last completion and uses a fixed
3600-second timeout for this nonstreaming endpoint.

Cargo-tool evaluator packages currently require the Local run environment.
Docker, Modal, and SkyPilot reject this task during evaluator provisioning.

## Run it

```bash
vibesys \
  --input examples/model-serving/whisper-large-v3 \
  --runs-dir /work/vibesys-runs --local \
  --exp-name whisper-offline \
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
