"""Compare two generated PNGs for pixel and perceptual drift."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rgb(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)


def box_filter(image: np.ndarray, size: int) -> np.ndarray:
    pad_before = size // 2
    pad_after = size - 1 - pad_before
    padded = np.pad(image, ((pad_before, pad_after), (pad_before, pad_after)), mode="reflect")
    integral = np.pad(padded, ((1, 0), (1, 0)), mode="constant").cumsum(axis=0).cumsum(axis=1)
    return (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    ) / float(size * size)


def rgb_to_luma(image: np.ndarray) -> np.ndarray:
    return image[..., 0] * 0.299 + image[..., 1] * 0.587 + image[..., 2] * 0.114


def local_ssim(a: np.ndarray, b: np.ndarray, window_size: int = 11) -> float | None:
    if a.shape != b.shape:
        return None
    if min(a.shape[:2]) < window_size:
        window_size = max(1, min(a.shape[:2]))
        if window_size % 2 == 0:
            window_size -= 1
    if window_size <= 1:
        return 1.0 if np.array_equal(a, b) else None

    x = rgb_to_luma(a)
    y = rgb_to_luma(b)
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2

    mu_x = box_filter(x, window_size)
    mu_y = box_filter(y, window_size)
    mu_x2 = mu_x * mu_x
    mu_y2 = mu_y * mu_y
    mu_xy = mu_x * mu_y

    sigma_x2 = box_filter(x * x, window_size) - mu_x2
    sigma_y2 = box_filter(y * y, window_size) - mu_y2
    sigma_xy = box_filter(x * y, window_size) - mu_xy

    numerator = (2.0 * mu_xy + c1) * (2.0 * sigma_xy + c2)
    denominator = (mu_x2 + mu_y2 + c1) * (sigma_x2 + sigma_y2 + c2)
    return float(np.mean(numerator / denominator))


def pixel_metrics(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    if a.shape != b.shape:
        return {
            "same_dimensions": False,
            "baseline_shape": list(a.shape),
            "candidate_shape": list(b.shape),
        }

    diff = a - b
    abs_diff = np.abs(diff)
    mse = float(np.mean(diff * diff))
    rmse = math.sqrt(mse)
    psnr = None if mse == 0.0 else 20.0 * math.log10(255.0 / rmse)
    changed_channels = abs_diff > 0.0
    changed_pixels = np.any(changed_channels, axis=2)
    return {
        "same_dimensions": True,
        "width": int(a.shape[1]),
        "height": int(a.shape[0]),
        "mae": float(np.mean(abs_diff)),
        "normalized_mae": float(np.mean(abs_diff) / 255.0),
        "rmse": rmse,
        "normalized_rmse": rmse / 255.0,
        "max_abs_diff": float(np.max(abs_diff)),
        "changed_channel_fraction": float(np.mean(changed_channels)),
        "changed_pixel_fraction": float(np.mean(changed_pixels)),
        "psnr_db": psnr,
        "psnr_is_infinite": mse == 0.0,
        "ssim_luma": local_ssim(a, b),
    }


def clip_metrics(
    baseline: Path,
    candidate: Path,
    prompt: str | None,
    model_name: str,
    allow_download: bool,
) -> dict[str, Any]:
    try:
        import torch
        from transformers import CLIPModel, CLIPProcessor
    except Exception as exc:  # pragma: no cover - depends on optional deps
        return {"enabled": False, "error": f"CLIP dependencies unavailable: {exc}"}

    try:
        processor = CLIPProcessor.from_pretrained(model_name, local_files_only=not allow_download)
        model = CLIPModel.from_pretrained(model_name, local_files_only=not allow_download)
    except Exception as exc:  # pragma: no cover - depends on local model cache
        return {"enabled": False, "error": f"CLIP model unavailable: {exc}"}

    images = [Image.open(baseline).convert("RGB"), Image.open(candidate).convert("RGB")]
    with torch.inference_mode():
        image_inputs = processor(images=images, return_tensors="pt")
        image_features = model.get_image_features(**image_inputs)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        image_cosine = float((image_features[0] * image_features[1]).sum().item())

        result: dict[str, Any] = {
            "enabled": True,
            "model": model_name,
            "image_cosine": image_cosine,
        }
        if prompt:
            text_inputs = processor(text=[prompt], return_tensors="pt", padding=True)
            text_features = model.get_text_features(**text_inputs)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            result["baseline_prompt_cosine"] = float((image_features[0] * text_features[0]).sum().item())
            result["candidate_prompt_cosine"] = float((image_features[1] * text_features[0]).sum().item())
    return result


def compare(args: argparse.Namespace) -> dict[str, Any]:
    baseline = Path(args.baseline)
    candidate = Path(args.candidate)
    baseline_array = load_rgb(baseline)
    candidate_array = load_rgb(candidate)
    result = {
        "baseline": str(baseline),
        "candidate": str(candidate),
        "baseline_sha256": sha256_file(baseline),
        "candidate_sha256": sha256_file(candidate),
        "exact_match": baseline.read_bytes() == candidate.read_bytes(),
        "pixel": pixel_metrics(baseline_array, candidate_array),
    }
    if args.clip_model:
        result["clip"] = clip_metrics(
            baseline,
            candidate,
            args.prompt,
            args.clip_model,
            args.allow_clip_download,
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two generated PNG images.")
    parser.add_argument("--baseline", required=True, help="Baseline PNG path")
    parser.add_argument("--candidate", required=True, help="Candidate PNG path")
    parser.add_argument("--prompt", default=None, help="Optional prompt for CLIP prompt-image scoring")
    parser.add_argument(
        "--clip-model",
        default=None,
        help="Optional local Hugging Face CLIP model id/path, for example openai/clip-vit-base-patch32",
    )
    parser.add_argument(
        "--allow-clip-download",
        action="store_true",
        help="Allow downloading the requested CLIP model if it is not already cached locally",
    )
    parser.add_argument("--output-json", default=None, help="Write structured comparison JSON")
    args = parser.parse_args()

    result = compare(args)
    print(json.dumps(result, indent=2))
    if args.output_json:
        Path(args.output_json).write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
