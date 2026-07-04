Accuracy checker for moonshine-streaming-medium.

Compares the custom server's `VibeServeModel.transcribe()` against HuggingFace `MoonshineStreamingModel` reference.  Uses real 16 kHz audio from `../test_audio/` plus ground-truth transcripts from `manifest.json`.

Run: `python checker.py --model-dir <local model dir>`

Pass criterion: word overlap ≥ 0.7 against the HF reference output, per sample.  Exit code 0 on full pass, 1 on any fail.
