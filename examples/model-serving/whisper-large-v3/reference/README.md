# Reference — whisper-large-v3

`reference.py` wraps the stock HuggingFace `WhisperForConditionalGeneration`
(native to `transformers`, no vendored modeling code needed) behind
`reference_transcribe(model, processor, audio, sampling_rate)` — greedy,
`<|en|><|transcribe|><|notimestamps|>`. The accuracy checker uses this as the
correctness ground truth the candidate must match.

- `meta.json` — model id (`openai/whisper-large-v3`) + pinned revision.
- `config.json` — the checkpoint config.

This directory is mounted read-only during a run: the Implementer cannot edit
the reference.
