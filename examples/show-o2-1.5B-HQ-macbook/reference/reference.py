"""Reference loader and inference wrapper for showlab/show-o2-1.5B-HQ.

The official Show-o2 repository is not packaged on PyPI, so this reference
bundle imports it from the pinned `Show-o` git submodule. Model weights stay
outside git and are resolved from Hugging Face using `meta.json`.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REFERENCE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_DIR = REFERENCE_DIR / "Show-o" / "show-o2"
DEFAULT_MODEL_DIR = REFERENCE_DIR / "model"
DEFAULT_RESOLUTION = 512


def _repo_root() -> Path:
    return REFERENCE_DIR.parent.parent.parent


def _read_meta() -> dict[str, Any]:
    return json.loads((REFERENCE_DIR / "meta.json").read_text())


def configure_environment_for_device(device: str) -> None:
    if device == "auto" or str(device).startswith("mps"):
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


def _add_source_to_path(source_dir: Path) -> None:
    source_dir = source_dir.resolve()
    if not (source_dir / "models").is_dir() or not (source_dir / "transport").is_dir():
        raise FileNotFoundError(f"Show-o2 source directory is incomplete: {source_dir}")
    source_text = str(source_dir)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _config_path_for_resolution(source_dir: Path, resolution: int) -> Path:
    configs = {
        432: "showo2_1.5b_demo_432x432.yaml",
        512: "showo2_1.5b_demo_512x512.yaml",
        1024: "showo2_1.5b_demo_1024x1024.yaml",
    }
    try:
        name = configs[int(resolution)]
    except KeyError as exc:
        raise ValueError(f"Unsupported Show-o2 resolution: {resolution}") from exc
    return source_dir / "configs" / name


def resolve_device(device: str = "auto"):
    import torch

    if device != "auto":
        return torch.device(device)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def empty_device_cache(device) -> None:
    import torch

    if device.type == "cuda":
        torch.cuda.empty_cache()
    elif device.type == "mps" and hasattr(torch, "mps"):
        torch.mps.empty_cache()


def synchronize_device(device) -> None:
    import torch

    if device.type == "cuda":
        torch.cuda.synchronize()
    elif device.type == "mps" and hasattr(torch, "mps") and hasattr(torch.mps, "synchronize"):
        torch.mps.synchronize()


def resolve_dtype(dtype: str = "auto", device=None):
    import torch

    if dtype == "auto":
        if device is not None and device.type == "mps":
            return torch.float16
        if device is not None and device.type == "cpu":
            return torch.float32
        return torch.bfloat16
    mapping = {
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float16": torch.float16,
        "fp16": torch.float16,
        "float32": torch.float32,
        "fp32": torch.float32,
    }
    try:
        return mapping[dtype]
    except KeyError as exc:
        raise ValueError(f"Unsupported dtype: {dtype}") from exc


def ensure_model_dir(model_dir: str | Path | None = None) -> Path:
    """Return a local Show-o2 checkpoint directory, downloading if needed."""
    if model_dir is not None:
        candidate = Path(model_dir).expanduser().resolve()
        if candidate.exists():
            return candidate
        if candidate != DEFAULT_MODEL_DIR.resolve():
            raise FileNotFoundError(f"Model directory does not exist: {candidate}")

    if DEFAULT_MODEL_DIR.exists():
        return DEFAULT_MODEL_DIR.resolve()

    meta = _read_meta()
    from huggingface_hub import snapshot_download

    downloaded = Path(
        snapshot_download(
            meta["model_id"],
            revision=meta.get("revision"),
            cache_dir=str(_repo_root() / ".hf_cache"),
        )
    )
    try:
        DEFAULT_MODEL_DIR.symlink_to(downloaded, target_is_directory=True)
        return DEFAULT_MODEL_DIR.resolve()
    except FileExistsError:
        return DEFAULT_MODEL_DIR.resolve()
    except OSError:
        return downloaded


def ensure_wan_vae(vae_path: str | Path | None = None) -> Path:
    """Return a local Wan2.1 VAE weight file, downloading if needed."""
    if vae_path is not None:
        candidate = Path(vae_path).expanduser().resolve()
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"Wan VAE path does not exist: {candidate}")

    local = REFERENCE_DIR / "Wan2.1_VAE.pth"
    if local.exists():
        return local.resolve()

    meta = _read_meta()["wan_vae"]
    from huggingface_hub import hf_hub_download

    return Path(
        hf_hub_download(
            repo_id=meta["model_id"],
            filename=meta["filename"],
            revision=meta.get("revision"),
            cache_dir=str(_repo_root() / ".hf_cache"),
        )
    )


def pil_to_png_bytes(image, *, compress_level: int | None = None) -> bytes:
    buffer = io.BytesIO()
    save_kwargs = {}
    if compress_level is not None:
        save_kwargs["compress_level"] = int(compress_level)
    image.save(buffer, format="PNG", **save_kwargs)
    return buffer.getvalue()


def pil_to_base64_png(image, *, compress_level: int | None = None) -> str:
    return base64.b64encode(
        pil_to_png_bytes(image, compress_level=compress_level),
    ).decode("ascii")


def resolve_secondary_device(device: str | None, fallback):
    if device in (None, "same"):
        return fallback
    return resolve_device(device)


def resolve_secondary_dtype(dtype: str | None, device, fallback):
    if dtype in (None, "same"):
        return fallback
    return resolve_dtype(dtype, device)


def denorm_cpu_first(images):
    import numpy as np

    arrays = images.detach().cpu().permute(0, 2, 3, 1).numpy()
    return np.clip((arrays + 1.0) * 127.5, 0.0, 255.0).astype(np.uint8)


def denorm_float32_first(images):
    import numpy as np
    import torch

    images = images.to(torch.float32)
    images = torch.clamp((images + 1.0) / 2.0, min=0.0, max=1.0)
    images *= 255.0
    return images.permute(0, 2, 3, 1).cpu().numpy().astype(np.uint8)


def denorm_native(images):
    import numpy as np
    import torch

    images = torch.clamp((images + 1.0) * 127.5, min=0.0, max=255.0)
    images = images.permute(0, 2, 3, 1).cpu()
    if images.dtype == torch.bfloat16:
        images = images.to(torch.float32)
    return images.numpy().astype(np.uint8)


def denorm_numpy(images):
    import numpy as np

    arrays = np.clip((images + 1.0) * 127.5, 0.0, 255.0)
    return np.transpose(arrays, (0, 2, 3, 1)).astype(np.uint8)


@dataclass
class ShowO2Model:
    model: Any
    tokenizer: Any
    showo_token_ids: dict[str, int]
    config: Any
    device: Any
    dtype: Any
    source_dir: Path
    resolution: int = DEFAULT_RESOLUTION
    vae_path: Path | None = None
    vae_model: Any | None = None
    vae_device: Any | None = None
    vae_dtype: Any | None = None
    vae_output_dtype: str = "float32"
    vae_decode_mode: str = "video"
    vae_conv2d_tail_start: int = 12
    vae_conv2d_tail_max_modules: int | None = None
    vae_upsample_mode: str = "default"
    vae_trace_decoder: bool = False
    vae_decoder_backend: str = "torch"
    vae_coreml_model_path: str | None = None
    vae_coreml_compute_units: str = "all"
    vae_coreml_optimization_hints: str = "none"
    vae_coreml_input_rank: int = 5
    vae_mlx_dtype: str = "float16"
    vae_mlx_compile: bool = True
    vae_mlx_low_rank_highres_rank: int = 64
    vae_mlx_low_rank_highres_min_size: int = 512
    vae_mlx_low_rank_highres_tail_rank: int | None = 36
    vae_mlx_low_rank_highres_tail_start_layer: int = 14
    vae_mlx_low_rank_override_layer: int | None = None
    vae_mlx_low_rank_override_conv_index: int | None = None
    vae_mlx_low_rank_override_rank: int | None = None
    vae_mlx_approx_highres_residual_start_layer: int | None = None
    vae_mlx_approx_highres_residual_end_layer: int | None = None
    vae_mlx_approx_highres_residual_mode: str = "full"
    vae_mlx_low_rank_pointwise_impl: str = "conv2d"
    vae_profile: bool = False
    postprocess_mode: str = "upstream"
    _sample_fn_cache: dict[tuple[int, int], Any] = field(default_factory=dict, repr=False)
    _attention_base_mask_cache: dict[tuple[int, int, str, str], Any] = field(
        default_factory=dict,
        repr=False,
    )
    _attention_mask_cache: dict[tuple[Any, ...], Any] = field(default_factory=dict, repr=False)
    _attention_mask_identity_cache: dict[tuple[Any, ...], Any] = field(default_factory=dict, repr=False)
    _prepared_prompt_cache: dict[tuple[Any, ...], tuple[Any, Any]] = field(
        default_factory=dict,
        repr=False,
    )
    last_image_timings_ms: dict[str, float] = field(default_factory=dict, repr=False)
    prewarm_timings_ms: dict[str, float] | None = field(default=None, repr=False)
    image_component_prewarm_timings_ms: dict[str, float] | None = field(default=None, repr=False)

    @classmethod
    def from_pretrained(
        cls,
        model_dir: str | Path | None = None,
        *,
        source_dir: str | Path | None = None,
        vae_path: str | Path | None = None,
        device: str = "auto",
        dtype: str = "auto",
        vae_device: str = "same",
        vae_dtype: str = "same",
        vae_output_dtype: str = "float32",
        vae_decode_mode: str = "video",
        vae_conv2d_tail_start: int = 12,
        vae_conv2d_tail_max_modules: int | None = None,
        vae_upsample_mode: str = "default",
        vae_trace_decoder: bool = False,
        vae_decoder_backend: str = "torch",
        vae_coreml_model_path: str | None = None,
        vae_coreml_compute_units: str = "all",
        vae_coreml_optimization_hints: str = "none",
        vae_coreml_input_rank: int = 5,
        vae_mlx_dtype: str = "float16",
        vae_mlx_compile: bool = True,
        vae_mlx_low_rank_highres_rank: int = 64,
        vae_mlx_low_rank_highres_min_size: int = 512,
        vae_mlx_low_rank_highres_tail_rank: int | None = 36,
        vae_mlx_low_rank_highres_tail_start_layer: int = 14,
        vae_mlx_low_rank_override_layer: int | None = None,
        vae_mlx_low_rank_override_conv_index: int | None = None,
        vae_mlx_low_rank_override_rank: int | None = None,
        vae_mlx_approx_highres_residual_start_layer: int | None = None,
        vae_mlx_approx_highres_residual_end_layer: int | None = None,
        vae_mlx_approx_highres_residual_mode: str = "full",
        vae_mlx_low_rank_pointwise_impl: str = "conv2d",
        vae_profile: bool = False,
        postprocess_mode: str = "upstream",
        resolution: int = DEFAULT_RESOLUTION,
        load_vae: bool = False,
    ) -> "ShowO2Model":
        if postprocess_mode not in {"upstream", "cpu", "native"}:
            raise ValueError(f"Unsupported postprocess mode: {postprocess_mode}")
        if vae_output_dtype not in {"float32", "native", "deferred"}:
            raise ValueError(f"Unsupported VAE output dtype: {vae_output_dtype}")
        if vae_decode_mode not in {"video", "image", "image-conv2d", "image-conv2d-tail"}:
            raise ValueError(f"Unsupported VAE decode mode: {vae_decode_mode}")
        if vae_conv2d_tail_start < 0:
            raise ValueError(f"Unsupported VAE conv2d tail start: {vae_conv2d_tail_start}")
        if vae_conv2d_tail_max_modules is not None and vae_conv2d_tail_max_modules < 0:
            raise ValueError(f"Unsupported VAE conv2d tail max modules: {vae_conv2d_tail_max_modules}")
        if vae_upsample_mode not in {"default", "convtranspose"}:
            raise ValueError(f"Unsupported VAE upsample mode: {vae_upsample_mode}")
        if vae_decoder_backend not in {"torch", "coreml", "mlx"}:
            raise ValueError(f"Unsupported VAE decoder backend: {vae_decoder_backend}")
        if vae_decoder_backend == "coreml" and not vae_coreml_model_path:
            raise ValueError("Core ML VAE decoder backend requires vae_coreml_model_path")
        if vae_coreml_input_rank not in (4, 5):
            raise ValueError(f"Unsupported Core ML VAE input rank: {vae_coreml_input_rank}")
        if vae_mlx_dtype not in {"float32", "float16", "bfloat16"}:
            raise ValueError(f"Unsupported MLX VAE dtype: {vae_mlx_dtype}")
        if vae_mlx_low_rank_highres_rank < 0:
            raise ValueError(f"Unsupported MLX low-rank high-res rank: {vae_mlx_low_rank_highres_rank}")
        if vae_mlx_low_rank_highres_min_size < 0:
            raise ValueError(
                f"Unsupported MLX low-rank high-res min size: {vae_mlx_low_rank_highres_min_size}"
            )
        if vae_mlx_low_rank_highres_tail_rank is not None and vae_mlx_low_rank_highres_tail_rank < 0:
            raise ValueError(
                f"Unsupported MLX low-rank high-res tail rank: {vae_mlx_low_rank_highres_tail_rank}"
            )
        if vae_mlx_low_rank_highres_tail_start_layer < 0:
            raise ValueError(
                "Unsupported MLX low-rank high-res tail start layer: "
                f"{vae_mlx_low_rank_highres_tail_start_layer}"
            )
        if vae_mlx_low_rank_override_layer is not None and vae_mlx_low_rank_override_layer < 0:
            raise ValueError(
                "Unsupported MLX low-rank override layer: "
                f"{vae_mlx_low_rank_override_layer}"
            )
        if (
            vae_mlx_low_rank_override_conv_index is not None
            and vae_mlx_low_rank_override_conv_index not in (0, 1)
        ):
            raise ValueError(
                "Unsupported MLX low-rank override conv index: "
                f"{vae_mlx_low_rank_override_conv_index}"
            )
        if vae_mlx_low_rank_override_rank is not None and vae_mlx_low_rank_override_rank < 0:
            raise ValueError(
                "Unsupported MLX low-rank override rank: "
                f"{vae_mlx_low_rank_override_rank}"
            )
        if (
            vae_mlx_approx_highres_residual_start_layer is not None
            and vae_mlx_approx_highres_residual_start_layer < 0
        ):
            raise ValueError(
                "Unsupported MLX approximate high-res residual start layer: "
                f"{vae_mlx_approx_highres_residual_start_layer}"
            )
        if (
            vae_mlx_approx_highres_residual_end_layer is not None
            and vae_mlx_approx_highres_residual_end_layer < 0
        ):
            raise ValueError(
                "Unsupported MLX approximate high-res residual end layer: "
                f"{vae_mlx_approx_highres_residual_end_layer}"
            )
        if vae_mlx_approx_highres_residual_mode not in {"full", "first-conv", "second-conv"}:
            raise ValueError(
                "Unsupported MLX approximate high-res residual mode: "
                f"{vae_mlx_approx_highres_residual_mode}"
            )
        if vae_mlx_low_rank_pointwise_impl not in {"conv2d", "matmul"}:
            raise ValueError(
                "Unsupported MLX low-rank pointwise implementation: "
                f"{vae_mlx_low_rank_pointwise_impl}"
            )
        configure_environment_for_device(device)
        configure_environment_for_device(vae_device)
        source = Path(source_dir or DEFAULT_SOURCE_DIR).expanduser().resolve()
        _add_source_to_path(source)

        from models import Showo2Qwen2_5
        from models.misc import get_text_tokenizer
        from omegaconf import OmegaConf
        from utils import get_hyper_params, path_to_llm_name

        resolved_model_dir = ensure_model_dir(model_dir)
        resolved_device = resolve_device(device)
        resolved_dtype = resolve_dtype(dtype, resolved_device)
        resolved_vae_device = resolve_secondary_device(vae_device, resolved_device)
        resolved_vae_dtype = resolve_secondary_dtype(vae_dtype, resolved_vae_device, resolved_dtype)
        if vae_decoder_backend == "mlx":
            import torch

            resolved_vae_device = torch.device("cpu")
            resolved_vae_dtype = torch.float32

        config = OmegaConf.load(_config_path_for_resolution(source, resolution))
        config.model.showo.pretrained_model_path = str(resolved_model_dir)

        tokenizer, showo_token_ids = get_text_tokenizer(
            config.model.showo.llm_model_path,
            add_showo_tokens=True,
            return_showo_token_ids=True,
            llm_name=path_to_llm_name[config.model.showo.llm_model_path],
        )
        config.model.showo.llm_vocab_size = len(tokenizer)

        if config.model.showo.add_time_embeds:
            config.dataset.preprocessing.num_t2i_image_tokens += 1
            config.dataset.preprocessing.num_mmu_image_tokens += 1
            config.dataset.preprocessing.num_video_tokens += 1

        model = Showo2Qwen2_5.from_pretrained(
            str(resolved_model_dir),
            use_safetensors=False,
        ).to(resolved_device)
        model.to(resolved_dtype)
        model.eval()

        runtime = cls(
            model=model,
            tokenizer=tokenizer,
            showo_token_ids=showo_token_ids,
            config=config,
            device=resolved_device,
            dtype=resolved_dtype,
            source_dir=source,
            resolution=resolution,
            vae_path=(
                ensure_wan_vae(vae_path)
                if load_vae and vae_decoder_backend != "coreml"
                else None
            ),
            vae_device=resolved_vae_device,
            vae_dtype=resolved_vae_dtype,
            vae_output_dtype=vae_output_dtype,
            vae_decode_mode=vae_decode_mode,
            vae_conv2d_tail_start=int(vae_conv2d_tail_start),
            vae_conv2d_tail_max_modules=vae_conv2d_tail_max_modules,
            vae_upsample_mode=vae_upsample_mode,
            vae_trace_decoder=vae_trace_decoder,
            vae_decoder_backend=vae_decoder_backend,
            vae_coreml_model_path=vae_coreml_model_path,
            vae_coreml_compute_units=vae_coreml_compute_units,
            vae_coreml_optimization_hints=vae_coreml_optimization_hints,
            vae_coreml_input_rank=int(vae_coreml_input_rank),
            vae_mlx_dtype=vae_mlx_dtype,
            vae_mlx_compile=bool(vae_mlx_compile),
            vae_mlx_low_rank_highres_rank=int(vae_mlx_low_rank_highres_rank),
            vae_mlx_low_rank_highres_min_size=int(vae_mlx_low_rank_highres_min_size),
            vae_mlx_low_rank_highres_tail_rank=(
                None
                if vae_mlx_low_rank_highres_tail_rank is None
                else int(vae_mlx_low_rank_highres_tail_rank)
            ),
            vae_mlx_low_rank_highres_tail_start_layer=int(vae_mlx_low_rank_highres_tail_start_layer),
            vae_mlx_low_rank_override_layer=(
                None
                if vae_mlx_low_rank_override_layer is None
                else int(vae_mlx_low_rank_override_layer)
            ),
            vae_mlx_low_rank_override_conv_index=(
                None
                if vae_mlx_low_rank_override_conv_index is None
                else int(vae_mlx_low_rank_override_conv_index)
            ),
            vae_mlx_low_rank_override_rank=(
                None
                if vae_mlx_low_rank_override_rank is None
                else int(vae_mlx_low_rank_override_rank)
            ),
            vae_mlx_approx_highres_residual_start_layer=(
                None
                if vae_mlx_approx_highres_residual_start_layer is None
                else int(vae_mlx_approx_highres_residual_start_layer)
            ),
            vae_mlx_approx_highres_residual_end_layer=(
                None
                if vae_mlx_approx_highres_residual_end_layer is None
                else int(vae_mlx_approx_highres_residual_end_layer)
            ),
            vae_mlx_approx_highres_residual_mode=vae_mlx_approx_highres_residual_mode,
            vae_mlx_low_rank_pointwise_impl=vae_mlx_low_rank_pointwise_impl,
            vae_profile=vae_profile,
            postprocess_mode=postprocess_mode,
        )
        runtime._hyper_params = get_hyper_params(config, tokenizer, showo_token_ids)
        if load_vae:
            runtime.load_vae()
        empty_device_cache(resolved_device)
        return runtime

    def load_vae(self) -> None:
        if self.vae_model is not None:
            return
        import torch

        _add_source_to_path(self.source_dir)
        from models import WanVAE

        if self.vae_decoder_backend == "coreml":
            vae_path = ""
        else:
            self.vae_path = self.vae_path or ensure_wan_vae()
            vae_path = str(self.vae_path)
        vae_device = "cpu" if self.vae_decoder_backend == "mlx" else (self.vae_device or self.device)
        vae_dtype = torch.float32 if self.vae_decoder_backend == "mlx" else (self.vae_dtype or self.dtype)
        self.vae_model = WanVAE(
            vae_pth=vae_path,
            dtype=vae_dtype,
            device=vae_device,
            output_dtype=self.vae_output_dtype,
            decode_mode=self.vae_decode_mode,
            conv2d_tail_start=self.vae_conv2d_tail_start,
            conv2d_tail_max_modules=self.vae_conv2d_tail_max_modules,
            upsample_mode=self.vae_upsample_mode,
            trace_decoder=self.vae_trace_decoder,
            decoder_backend=self.vae_decoder_backend,
            coreml_model_path=self.vae_coreml_model_path,
            coreml_compute_units=self.vae_coreml_compute_units,
            coreml_optimization_hints=self.vae_coreml_optimization_hints,
            coreml_input_rank=self.vae_coreml_input_rank,
            coreml_latent_size=self.resolution // 8,
            mlx_dtype=self.vae_mlx_dtype,
            mlx_compile=self.vae_mlx_compile,
            mlx_low_rank_highres_rank=self.vae_mlx_low_rank_highres_rank,
            mlx_low_rank_highres_min_size=self.vae_mlx_low_rank_highres_min_size,
            mlx_low_rank_highres_tail_rank=self.vae_mlx_low_rank_highres_tail_rank,
            mlx_low_rank_highres_tail_start_layer=self.vae_mlx_low_rank_highres_tail_start_layer,
            mlx_low_rank_override_layer=self.vae_mlx_low_rank_override_layer,
            mlx_low_rank_override_conv_index=self.vae_mlx_low_rank_override_conv_index,
            mlx_low_rank_override_rank=self.vae_mlx_low_rank_override_rank,
            mlx_approx_highres_residual_start_layer=self.vae_mlx_approx_highres_residual_start_layer,
            mlx_approx_highres_residual_end_layer=self.vae_mlx_approx_highres_residual_end_layer,
            mlx_approx_highres_residual_mode=self.vae_mlx_approx_highres_residual_mode,
            mlx_low_rank_pointwise_impl=self.vae_mlx_low_rank_pointwise_impl,
            mlx_latent_size=self.resolution // 8,
            profile=self.vae_profile,
        )

    def prewarm_vae_decoder(self) -> dict[str, float]:
        import torch

        timings: dict[str, float] = {}
        total_started = time.perf_counter()

        load_started = time.perf_counter()
        self.load_vae()
        timings["load_vae_ms"] = (time.perf_counter() - load_started) * 1000.0

        hyper_params = getattr(self, "_hyper_params", None)
        if hyper_params is not None:
            image_latent_dim = int(hyper_params[5])
            patch_size = int(hyper_params[6])
            latent_width = int(hyper_params[7]) * patch_size
            latent_height = int(hyper_params[8]) * patch_size
        else:
            image_latent_dim = 16
            latent_width = latent_height = self.resolution // 8

        if self.vae_decoder_backend in {"coreml", "mlx"}:
            latent_device = "cpu"
            latent_dtype = torch.float32
        else:
            latent_device = self.vae_device or self.device
            latent_dtype = self.vae_dtype or self.dtype
        latents = torch.zeros(
            (1, image_latent_dim, 1, latent_height, latent_width),
            device=latent_device,
            dtype=latent_dtype,
        )

        decode_started = time.perf_counter()
        with torch.inference_mode():
            decoded = self.vae_model.batch_decode(latents)
            if self.vae_decoder_backend not in {"coreml", "mlx"}:
                synchronize_device(torch.device(self.vae_device or self.device))
        timings["decode_ms"] = (time.perf_counter() - decode_started) * 1000.0
        timings["total_ms"] = (time.perf_counter() - total_started) * 1000.0

        del decoded, latents
        self.prewarm_timings_ms = timings
        return timings

    def prewarm_image_components(
        self,
        prompt: str,
        *,
        num_inference_steps: int = 1,
        guidance_scale: float = 5.0,
    ) -> dict[str, float]:
        total_started = time.perf_counter()
        timings: dict[str, float] = {}

        (
            num_t2i_image_tokens,
            _num_mmu_image_tokens,
            _num_video_tokens,
            max_seq_len,
            max_text_len,
            _image_latent_dim,
            _patch_size,
            _latent_width,
            _latent_height,
            pad_id,
            bos_id,
            eos_id,
            boi_id,
            eoi_id,
            _bov_id,
            _eov_id,
            img_pad_id,
            _vid_pad_id,
            _default_guidance_scale,
        ) = self._hyper_params

        prepare_started = time.perf_counter()
        text_tokens, positions, prompt_cache_hit = self._get_prepared_prompt_inputs(
            prompt,
            guidance_scale=guidance_scale,
            num_t2i_image_tokens=num_t2i_image_tokens,
            max_text_len=max_text_len,
            bos_id=bos_id,
            eos_id=eos_id,
            boi_id=boi_id,
            eoi_id=eoi_id,
            pad_id=pad_id,
            img_pad_id=img_pad_id,
        )
        timings["prepare_ms"] = (time.perf_counter() - prepare_started) * 1000.0
        timings["prepare_cache_hit"] = 1.0 if prompt_cache_hit else 0.0

        mask_started = time.perf_counter()
        _attention_mask, mask_cache_hit = self._get_attention_mask(
            text_tokens.size(0),
            max_seq_len,
            positions,
            self.dtype,
        )
        timings["mask_ms"] = (time.perf_counter() - mask_started) * 1000.0
        timings["mask_cache_hit"] = 1.0 if mask_cache_hit else 0.0

        sampler_started = time.perf_counter()
        self._get_sample_fn(num_inference_steps, num_t2i_image_tokens)
        timings["sampler_setup_ms"] = (time.perf_counter() - sampler_started) * 1000.0
        timings["total_ms"] = (time.perf_counter() - total_started) * 1000.0
        self.image_component_prewarm_timings_ms = timings
        return timings

    def _prepared_prompt_cache_key(
        self,
        prompt: str,
        *,
        guidance_scale: float,
        num_t2i_image_tokens: int,
        max_text_len: int,
    ) -> tuple[Any, ...]:
        return (
            prompt,
            bool(guidance_scale > 0),
            int(num_t2i_image_tokens),
            int(max_text_len),
            str(self.device),
        )

    def _get_prepared_prompt_inputs(
        self,
        prompt: str,
        *,
        guidance_scale: float,
        num_t2i_image_tokens: int,
        max_text_len: int,
        bos_id: int,
        eos_id: int,
        boi_id: int,
        eoi_id: int,
        pad_id: int,
        img_pad_id: int,
    ):
        import torch
        from models.misc import prepare_gen_input

        cache_key = self._prepared_prompt_cache_key(
            prompt,
            guidance_scale=guidance_scale,
            num_t2i_image_tokens=num_t2i_image_tokens,
            max_text_len=max_text_len,
        )
        cached = self._prepared_prompt_cache.get(cache_key)
        if cached is not None:
            text_tokens, positions = cached
            return text_tokens, positions, True

        text_tokens, text_tokens_null, positions, positions_null = prepare_gen_input(
            [prompt],
            self.tokenizer,
            num_t2i_image_tokens,
            bos_id,
            eos_id,
            boi_id,
            eoi_id,
            pad_id,
            img_pad_id,
            max_text_len,
            self.device,
        )
        if guidance_scale > 0:
            text_tokens = torch.cat([text_tokens, text_tokens_null], dim=0)
            positions = torch.cat([positions, positions_null], dim=0)
        self._prepared_prompt_cache[cache_key] = (text_tokens, positions)
        return text_tokens, positions, False

    def _get_sample_fn(self, num_inference_steps: int, num_t2i_image_tokens: int):
        from transport import Sampler, create_transport

        key = (int(num_inference_steps), int(num_t2i_image_tokens))
        sample_fn = self._sample_fn_cache.get(key)
        if sample_fn is not None:
            return sample_fn

        transport = create_transport(
            path_type=self.config.transport.path_type,
            prediction=self.config.transport.prediction,
            loss_weight=self.config.transport.loss_weight,
            train_eps=self.config.transport.train_eps,
            sample_eps=self.config.transport.sample_eps,
            snr_type=self.config.transport.snr_type,
            do_shift=self.config.transport.do_shift,
            seq_len=num_t2i_image_tokens,
        )
        sampler = Sampler(transport)
        sample_fn = sampler.sample_ode(
            sampling_method=self.config.transport.sampling_method,
            num_steps=num_inference_steps,
            atol=self.config.transport.atol,
            rtol=self.config.transport.rtol,
            reverse=self.config.transport.reverse,
            time_shifting_factor=self.config.transport.time_shifting_factor,
        )
        self._sample_fn_cache[key] = sample_fn
        return sample_fn

    @staticmethod
    def _limited_cache_set(cache: dict, key: tuple[Any, ...], value: Any, *, limit: int = 64) -> None:
        if len(cache) >= limit:
            cache.clear()
        cache[key] = value

    def _get_base_attention_mask(self, batch_size: int, max_seq_len: int, dtype):
        import torch

        key = (int(batch_size), int(max_seq_len), str(self.device), str(dtype))
        cached = self._attention_base_mask_cache.get(key)
        if cached is not None:
            return cached, True

        blocked_value = torch.tensor(torch.iinfo(torch.long).min, dtype=dtype, device=self.device)
        mask = torch.zeros(
            (int(batch_size), 1, int(max_seq_len), int(max_seq_len)),
            dtype=dtype,
            device=self.device,
        )
        upper_triangle = torch.ones(
            (int(max_seq_len), int(max_seq_len)),
            dtype=torch.bool,
            device=self.device,
        ).triu(diagonal=1)
        mask.masked_fill_(upper_triangle.view(1, 1, int(max_seq_len), int(max_seq_len)), blocked_value)
        self._limited_cache_set(self._attention_base_mask_cache, key, mask, limit=16)
        return mask, False

    def _get_attention_mask(self, batch_size: int, max_seq_len: int, positions, dtype):
        base_key = (int(batch_size), int(max_seq_len), str(self.device), str(dtype))
        identity_key = (*base_key, positions)
        cached = self._attention_mask_identity_cache.get(identity_key)
        if cached is not None:
            return cached, True

        position_values = tuple(int(value) for value in positions.detach().cpu().reshape(-1).tolist())
        key = (*base_key, position_values)
        cached = self._attention_mask_cache.get(key)
        if cached is not None:
            self._limited_cache_set(self._attention_mask_identity_cache, identity_key, cached)
            return cached, True

        base_mask, _base_hit = self._get_base_attention_mask(batch_size, max_seq_len, dtype)
        mask = base_mask.clone()
        for batch_index, modality_batch in enumerate(positions.detach().cpu().tolist()):
            for offset, length in modality_batch:
                offset = int(offset)
                length = int(length)
                if length > 0:
                    mask[
                        batch_index,
                        :,
                        offset:offset + length,
                        offset:offset + length,
                    ] = 0
        self._limited_cache_set(self._attention_mask_cache, key, mask)
        self._limited_cache_set(self._attention_mask_identity_cache, identity_key, mask)
        return mask, False

    def generate_text(
        self,
        prompt: str,
        *,
        max_new_tokens: int = 64,
        temperature: float = 0.0,
        top_k: int | None = None,
    ) -> str:
        import torch

        token_ids = [self.showo_token_ids["bos_id"]]
        token_ids.extend(self.tokenizer(prompt, add_special_tokens=False).input_ids)
        output_ids: list[int] = []

        with torch.inference_mode():
            for _ in range(max_new_tokens):
                input_ids = torch.tensor([token_ids], device=self.device)
                outputs = self.model.showo(input_ids=input_ids, return_dict=True)
                logits = outputs.logits[:, -1, :]
                if top_k is not None:
                    values, _ = torch.topk(logits, min(top_k, logits.shape[-1]))
                    logits = torch.where(
                        logits < values[:, [-1]],
                        torch.full_like(logits, float("-inf")),
                        logits,
                    )
                if temperature <= 0:
                    next_token = int(torch.argmax(logits, dim=-1).item())
                else:
                    probs = torch.softmax(logits / temperature, dim=-1)
                    next_token = int(torch.multinomial(probs, num_samples=1).item())
                if next_token == self.tokenizer.eos_token_id:
                    break
                token_ids.append(next_token)
                output_ids.append(next_token)

        return self.tokenizer.decode(output_ids, skip_special_tokens=True)

    def generate_image(
        self,
        prompt: str,
        *,
        num_inference_steps: int = 20,
        guidance_scale: float = 5.0,
        seed: int | None = None,
        postprocess_mode: str | None = None,
        return_arrays: bool = False,
    ):
        import torch
        from PIL import Image
        from utils import denorm

        total_started = time.perf_counter()
        active_postprocess_mode = postprocess_mode or self.postprocess_mode
        if active_postprocess_mode not in {"upstream", "cpu", "native"}:
            raise ValueError(f"Unsupported postprocess mode: {active_postprocess_mode}")
        timings: dict[str, float] = {}
        load_vae_started = time.perf_counter()
        self.load_vae()
        timings["load_vae_ms"] = (time.perf_counter() - load_vae_started) * 1000.0
        if seed is not None:
            torch.manual_seed(seed)
            if self.device.type == "cuda":
                torch.cuda.manual_seed_all(seed)
            elif self.device.type == "mps" and hasattr(torch, "mps") and hasattr(torch.mps, "manual_seed"):
                torch.mps.manual_seed(seed)

        (
            num_t2i_image_tokens,
            _num_mmu_image_tokens,
            _num_video_tokens,
            max_seq_len,
            max_text_len,
            image_latent_dim,
            patch_size,
            latent_width,
            latent_height,
            pad_id,
            bos_id,
            eos_id,
            boi_id,
            eoi_id,
            _bov_id,
            _eov_id,
            img_pad_id,
            _vid_pad_id,
            _default_guidance_scale,
        ) = self._hyper_params

        with torch.inference_mode():
            prepare_started = time.perf_counter()
            text_tokens, positions, prompt_cache_hit = self._get_prepared_prompt_inputs(
                prompt,
                guidance_scale=guidance_scale,
                num_t2i_image_tokens=num_t2i_image_tokens,
                max_text_len=max_text_len,
                bos_id=bos_id,
                eos_id=eos_id,
                boi_id=boi_id,
                eoi_id=eoi_id,
                pad_id=pad_id,
                img_pad_id=img_pad_id,
            )
            timings["prepare_ms"] = (time.perf_counter() - prepare_started) * 1000.0
            timings["prepare_cache_hit"] = 1.0 if prompt_cache_hit else 0.0

            latent_started = time.perf_counter()
            z = torch.randn(
                (
                    1,
                    image_latent_dim,
                    latent_height * patch_size,
                    latent_width * patch_size,
                ),
                device=self.device,
                dtype=self.dtype,
            )

            if guidance_scale > 0:
                z = torch.cat([z, z], dim=0)
            timings["latent_ms"] = (time.perf_counter() - latent_started) * 1000.0

            mask_started = time.perf_counter()
            attention_mask, mask_cache_hit = self._get_attention_mask(
                text_tokens.size(0),
                max_seq_len,
                positions,
                self.dtype,
            )
            timings["mask_ms"] = (time.perf_counter() - mask_started) * 1000.0
            timings["mask_cache_hit"] = 1.0 if mask_cache_hit else 0.0

            sampler_started = time.perf_counter()
            sample_fn = None if num_inference_steps == 1 else self._get_sample_fn(
                num_inference_steps,
                num_t2i_image_tokens,
            )
            timings["sampler_setup_ms"] = (time.perf_counter() - sampler_started) * 1000.0

            sample_started = time.perf_counter()
            if sample_fn is None:
                samples = z.float()
            else:
                samples = sample_fn(
                    z,
                    self.model.t2i_generate,
                    text_tokens=text_tokens,
                    attention_mask=attention_mask,
                    modality_positions=positions,
                    output_hidden_states=True,
                    max_seq_len=max_seq_len,
                    guidance_scale=guidance_scale,
                )[-1]

            if guidance_scale > 0:
                samples = torch.chunk(samples, 2)[0]
            timings["sample_ms"] = (time.perf_counter() - sample_started) * 1000.0

            decode_started = time.perf_counter()
            samples = samples.unsqueeze(2)
            if self.vae_decoder_backend == "mlx":
                decoded = self.vae_model.batch_decode_mlx(samples, output_layout="nhwc_uint8")
                images = decoded
            else:
                decoded = self.vae_model.batch_decode(samples)
                images = decoded.squeeze(2) if decoded.ndim == 5 else decoded
            timings["vae_decode_ms"] = (time.perf_counter() - decode_started) * 1000.0
            profile_timings = getattr(self.vae_model, "last_profile_timings_ms", None)
            if isinstance(profile_timings, dict):
                timings.update(profile_timings)

            postprocess_started = time.perf_counter()
            if self.vae_decoder_backend == "mlx":
                arrays = images
            elif self.vae_decoder_backend == "coreml":
                arrays = denorm_numpy(images)
            elif active_postprocess_mode == "cpu":
                arrays = denorm_cpu_first(images)
            elif active_postprocess_mode == "native":
                arrays = denorm_native(images)
            elif self.vae_output_dtype == "deferred":
                arrays = denorm_float32_first(images)
            else:
                arrays = denorm(images)
            if self.vae_decoder_backend not in {"coreml", "mlx"}:
                synchronize_device(self.device)
            timings["postprocess_ms"] = (time.perf_counter() - postprocess_started) * 1000.0

        pil_started = time.perf_counter()
        result = list(arrays) if return_arrays else [Image.fromarray(image) for image in arrays]
        timings["pil_ms"] = (time.perf_counter() - pil_started) * 1000.0
        timings["total_ms"] = (time.perf_counter() - total_started) * 1000.0
        self.last_image_timings_ms = timings
        return result
