# Accuracy checker — whisper-large-v3

`checker.py` loads the HF reference (`reference/reference.py`) and the candidate
(`main.py:VibeServeModel`), transcribes every `test_audio/` clip with both, and
gates each on normalized word-overlap against the reference (exact match, or
overlap ≥ `--threshold`, default 0.9). Exit 0 iff every clip passes.

```bash
uv run python accuracy_checker/checker.py --model-dir ../model --threshold 0.9
```

The gate is reference-vs-candidate (not candidate-vs-ground-truth), so passing
means *reproducing the HF implementation's transcripts*, independent of
whisper's own residual WER.
