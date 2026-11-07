# Reference — whisper-large-v3

`reference.py` wraps the stock HuggingFace `WhisperForConditionalGeneration`
(native to `transformers`, no vendored modeling code needed) behind
`reference_transcribe(model, processor, audio, sampling_rate)` — greedy,
`<|en|><|transcribe|><|notimestamps|>`. Its transcripts of the test clips, made
by `../accuracy_checker/make_references.py`, are the correctness ground truth
the accuracy checker holds the candidate to.

- `meta.json` — model id (`openai/whisper-large-v3`) + pinned revision.
- `config.json` — the checkpoint config.

This directory is mounted read-only during a run: the Implementer cannot edit
the reference.
