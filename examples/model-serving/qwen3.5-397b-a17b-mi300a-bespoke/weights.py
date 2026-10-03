"""Lazy safetensors access over a directory of shards."""

from pathlib import Path

import torch
from safetensors import safe_open


class Checkpoint:
    """Maps tensor name -> shard by reading shard headers only; tensors load on demand."""

    def __init__(self, path: str | Path) -> None:
        self.files = sorted(Path(path).glob("*.safetensors"))
        if not self.files:
            raise FileNotFoundError(f"no *.safetensors files under {path}")
        self._handles: dict[Path, object] = {}
        self._owner: dict[str, Path] = {}
        for f in self.files:
            for name in self._handle(f).keys():
                self._owner[name] = f

    def _handle(self, f: Path) -> object:
        if f not in self._handles:
            self._handles[f] = safe_open(str(f), framework="pt", device="cpu")
        return self._handles[f]

    def has(self, name: str) -> bool:
        return name in self._owner

    def load(
        self, name: str, device: str | torch.device, dtype: torch.dtype | None = None
    ) -> torch.Tensor:
        """Read one tensor (CPU mmap), move it to `device`, cast to `dtype` unless None."""
        tensor = self._handle(self._owner[name]).get_tensor(name)
        return tensor.to(device=device, dtype=dtype or tensor.dtype)
