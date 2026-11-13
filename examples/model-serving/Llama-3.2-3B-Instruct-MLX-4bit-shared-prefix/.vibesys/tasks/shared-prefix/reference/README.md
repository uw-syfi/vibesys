# Stock MLX-LM reference

`meta.json` pins the existing 3B 4-bit model. No weights are stored here.
Set `MLX_MODEL_PATH` to its downloaded Hugging Face snapshot; resolution validates
the pinned snapshot revision and weight configuration and never downloads.

The initial candidate delegates HTTP serving to MLX-LM 0.31.3 with native defaults:
32 decode concurrency, 8 prompt concurrency, prefill step 2048, prompt cache
size 10, and no explicit cache-byte cap. These settings describe the baseline,
not constraints on subsequent serving implementations.

Before a future VibeSys run in its separate candidate repository, create an
ignored `model` symlink here to the existing snapshot and enforce
`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`. VibeSys's model preparation otherwise
may try to populate its own cache. Do not create that repository or start a run
as part of this implementation phase.

Stock streaming usage reports actual completion tokens and cached prompt tokens.
It does not expose HTTP prompt-processing duration or Metal peak allocation.
The evaluator reports process peak RSS, including startup and warmup, and marks
the unavailable diagnostics explicitly. No offline generation timing is mixed
into the HTTP baseline.
