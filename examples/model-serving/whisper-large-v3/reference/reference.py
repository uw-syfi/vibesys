# Copyright 2026. Licensed under the Apache License, Version 2.0.
"""Reference transcription for whisper-large-v3.

whisper-large-v3 is native to `transformers` (`WhisperForConditionalGeneration`),
so — unlike a model whose modeling code must be vendored — the reference here is
the stock HuggingFace implementation driven through `generate()`. This module
wraps it behind a single `reference_transcribe` used by the accuracy checker as
the correctness ground truth.

Greedy, `<|en|>` + `<|transcribe|>` + `<|notimestamps|>`, as the offline-
transcribe contract the candidate `VibeServeModel.transcribe` must match.
"""

from __future__ import annotations

import numpy as np
import torch
from transformers import WhisperForConditionalGeneration, WhisperProcessor

_SAMPLE_RATE = 16000


def load_reference(model_dir: str, device: str, dtype: torch.dtype):
    processor = WhisperProcessor.from_pretrained(model_dir)
    model = (
        WhisperForConditionalGeneration.from_pretrained(model_dir, torch_dtype=dtype)
        .to(device)
        .eval()
    )
    return model, processor


@torch.inference_mode()
def reference_transcribe(
    model: WhisperForConditionalGeneration,
    processor: WhisperProcessor,
    audio: np.ndarray,
    sampling_rate: int = _SAMPLE_RATE,
    max_new_tokens: int = 256,
) -> str:
    device = next(model.parameters()).device
    dtype = next(model.parameters()).dtype
    feats = processor(
        audio, sampling_rate=sampling_rate, return_tensors="pt"
    ).input_features.to(device=device, dtype=dtype)
    ids = model.generate(
        feats,
        language="en",
        task="transcribe",
        do_sample=False,
        num_beams=1,
        max_new_tokens=max_new_tokens,
    )
    return processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
