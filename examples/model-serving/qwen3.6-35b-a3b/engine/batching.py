"""Join several requests' model caches into one batch for a decode step, and split them again.

Each request owns a Transformers ``DynamicCache`` holding Gated DeltaNet layers
(convolution and recurrent states, one row per sequence) and attention layers
(keys and values of shape ``[batch, heads, length, head_dim]``). ``merge``
stacks the rows; attention keys and values of shorter requests are left-padded
with zeros, which ``padding_mask`` hides from attention. ``split`` hands each
request back its own rows after the step. This reads and writes the cache
layers' fields directly (``keys``, ``values``, ``conv_states``,
``recurrent_states``), which is why Transformers is pinned to one version.
"""

from __future__ import annotations

import copy

import torch
from transformers import DynamicCache
from transformers.cache_utils import DynamicLayer, LinearAttentionCacheLayerMixin


def _states(layers: list, field: str) -> dict:
    first = getattr(layers[0], field)
    return {
        index: None
        if state is None
        else torch.cat([getattr(layer, field)[index] for layer in layers])
        for index, state in first.items()
    }


def merge(caches: list[DynamicCache]) -> tuple[DynamicCache, list[int]]:
    """One cache holding every request's rows, and each request's own length."""
    lengths = [cache.get_seq_length() for cache in caches]
    longest = max(lengths)
    merged = copy.copy(caches[0])
    merged.layers = []
    for index, first in enumerate(caches[0].layers):
        parts = [cache.layers[index] for cache in caches]
        layer = copy.copy(first)
        if isinstance(first, LinearAttentionCacheLayerMixin):
            layer.conv_states = _states(parts, "conv_states")
            layer.recurrent_states = _states(parts, "recurrent_states")
            layer.has_previous_state = dict(first.has_previous_state)
        if isinstance(first, DynamicLayer):
            for field in ("keys", "values"):
                rows = [getattr(part, field) for part in parts]
                batch, heads, _, dim = rows[0].shape
                padded = rows[0].new_zeros((len(rows), heads, longest, dim))
                for row, (tensor, length) in enumerate(zip(rows, lengths, strict=True)):
                    padded[row, :, longest - length :] = tensor[0]
                setattr(layer, field, padded)
        merged.layers.append(layer)
    return merged, lengths


def padding_mask(lengths: list[int], device: torch.device) -> torch.Tensor:
    """Attention mask for one new token per request on top of ``merge``'s left-padded cache."""
    longest = max(lengths)
    mask = torch.zeros((len(lengths), longest + 1), dtype=torch.long)
    for row, length in enumerate(lengths):
        mask[row, longest - length :] = 1
    return mask.to(device)


def split(merged: DynamicCache, lengths: list[int]) -> list[DynamicCache]:
    """Each request's rows of ``merged`` after a step that added one token to every request."""
    longest = max(lengths)
    caches = []
    for row, length in enumerate(lengths):
        cache = copy.copy(merged)
        cache.layers = []
        for merged_layer in merged.layers:
            layer = copy.copy(merged_layer)
            if isinstance(merged_layer, LinearAttentionCacheLayerMixin):
                for field in ("conv_states", "recurrent_states"):
                    states = getattr(merged_layer, field)
                    setattr(
                        layer,
                        field,
                        {i: None if s is None else s[row : row + 1] for i, s in states.items()},
                    )
                layer.has_previous_state = dict(merged_layer.has_previous_state)
            if isinstance(merged_layer, DynamicLayer):
                layer.keys = merged_layer.keys[row : row + 1, :, longest - length :]
                layer.values = merged_layer.values[row : row + 1, :, longest - length :]
            cache.layers.append(layer)
        caches.append(cache)
    return caches
