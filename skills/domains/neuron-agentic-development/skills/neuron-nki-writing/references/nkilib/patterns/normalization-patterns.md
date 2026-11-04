# Normalization Patterns

## Overview
Reusable patterns for normalization kernel data loading and shape validation in token-generation mode. These utilities handle the HBM-to-SBUF data movement and layout transformations required for RMSNorm/LayerNorm operations, including support for hidden-dimension sharding and transpose loading.

## Quick Reference

| Function | Signature | Description |
|----------|-----------|-------------|
| `validate_shapes` | `(input_view, gamma_view, output_view) -> (BxS, H, H0, H1)` | Validate and extract normalization tensor dimensions |
| `load_input_to_sbuf` | `(input_hbm, input_sb, num_H_shards, hidden_dim_tp) -> TensorView` | Load input from HBM to SBUF with layout transformation |
| `load_gamma_to_sbuf` | `(gamma_hbm, gamma_sb, num_H_shards, hidden_dim_tp) -> TensorView` | Load gamma weights from HBM to SBUF with layout transformation |

## Import Options

**Default** — inline the source into your kernel file.
See the "Full Source Implementation" section below, or the bundled source files in `references/nkilib/core/`.

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.subkernels.norm_tkg_utils import validate_shapes, load_input_to_sbuf, load_gamma_to_sbuf
```

## API Documentation

### `validate_shapes(input_view, gamma_view, output_view) -> Tuple[int, int, int, int]`

Validate tensor shapes for normalization operations. Handles both HBM inputs (shape `[B, S, H]`) and SBUF inputs (shape `[H0, BxS, H1]`).

**Args:**
- `input_view` (TensorView): Input tensor view. HBM shape `[B, S, H]` or SBUF shape `[H0, BxS, H1]`
- `gamma_view` (TensorView): Gamma tensor view with shape `[1, H]`
- `output_view` (TensorView): Output tensor view with expected shape `[H0, BxS, H1]`

**Returns:**
- `Tuple[int, int, int, int]`: `(BxS, H, H0, H1)` dimensions where:
  - `BxS`: Flattened batch-times-sequence dimension
  - `H`: Full hidden dimension
  - `H0`: Partition dimension (always `nl.tile_size.pmax` = 128)
  - `H1`: Hidden dimension tiles (`H // H0`)

**Constraints:**
- `H0` must equal `nl.tile_size.pmax` (128)
- `H` must be divisible by `H0`
- Output shape must be `[H0, BxS, H1]`
- Gamma shape must be `[1, H]`

**Example:**
```python
from nkilib.core.utils.tensor_view import TensorView

input_view = TensorView(input_tensor)   # [B, S, H] in HBM
gamma_view = TensorView(gamma_tensor)   # [1, H]
output_view = TensorView(output_buf)    # [H0, BxS, H1] in SBUF

BxS, H, H0, H1 = validate_shapes(input_view, gamma_view, output_view)
# H0 = 128, H1 = H // 128
```

---

### `load_input_to_sbuf(input_hbm, input_sb, num_H_shards, hidden_dim_tp=False) -> TensorView`

Load input data from HBM to SBUF with appropriate layout transformation. Supports two loading strategies depending on the hidden dimension layout.

**Args:**
- `input_hbm` (TensorView): Input tensor view in HBM with shape `[BxS, H]`
- `input_sb` (TensorView): Destination buffer in SBUF with shape `[H0, BxS, H1]`
- `num_H_shards` (int): Number of shards along the H dimension
- `hidden_dim_tp` (bool): If True, use transpose load for `(H/128, 128)` layout. Default: False

**Returns:**
- `TensorView`: Input tensor view in SBUF with shape `[H0, BxS, H1]`

**Constraints:**
- `H0` is always `nl.tile_size.pmax` (128)
- `H` must be divisible by `H0`
- `H1` must be divisible by `num_H_shards`

**Notes:**
- `hidden_dim_tp=True`: Uses `dma_transpose` for `(BxS, H) -> (BxS*H1, H0) -> (H0, BxS, H1)` transformation
- `hidden_dim_tp=False`: Uses `dma_copy` with permutation for `(BxS, H) -> (BxS, num_H_shards, H0, H2) -> (H0, BxS, num_H_shards, H2)` transformation
- Static DMA mode (`_DGE_MODE_NONE = 3`) is used for the non-transpose path

**Example:**
```python
import nki.language as nl

H0 = nl.tile_size.pmax  # 128
BxS = batch_size * seq_len
H1 = hidden_size // H0

input_sb = nl.ndarray((H0, BxS, H1), dtype=input_hbm.dtype, buffer=nl.sbuf)
input_view = load_input_to_sbuf(
    TensorView(input_hbm),
    TensorView(input_sb),
    num_H_shards=1,
    hidden_dim_tp=False,
)
```

---

### `load_gamma_to_sbuf(gamma_hbm, gamma_sb, num_H_shards, hidden_dim_tp=False) -> TensorView`

Load gamma (scale) weights from HBM to SBUF with appropriate layout transformation. Follows the same layout strategy as `load_input_to_sbuf` but for 1D gamma vectors.

**Args:**
- `gamma_hbm` (TensorView): Gamma tensor view in HBM with shape `[1, H]`
- `gamma_sb` (TensorView): Destination buffer in SBUF with shape `[H0, H1]`
- `num_H_shards` (int): Number of shards along the H dimension
- `hidden_dim_tp` (bool): If True, use transpose load. Default: False

**Returns:**
- `TensorView`: Gamma tensor view in SBUF with shape `[H0, H1]`

**Constraints:**
- Gamma must have shape `[1, H]` in HBM
- `H` must be divisible by `H0` (128)
- `H1` must be divisible by `num_H_shards`

**Notes:**
- `hidden_dim_tp=True`: Transpose load `(H) -> (H1, H0) -> (H0, H1)`
- `hidden_dim_tp=False`: Standard layout `(H) -> (num_H_shards, H0, H2) -> (H0, num_H_shards, H2)`

**Example:**
```python
import nki.language as nl

H0 = nl.tile_size.pmax  # 128
H1 = hidden_size // H0

gamma_sb = nl.ndarray((H0, H1), dtype=gamma_hbm.dtype, buffer=nl.sbuf)
gamma_view = load_gamma_to_sbuf(
    TensorView(gamma_hbm),
    TensorView(gamma_sb),
    num_H_shards=1,
    hidden_dim_tp=False,
)
```

## Usage Examples

### Pattern 1: Standard normalization data preparation
```python
import nki.language as nl
from nkilib.core.utils.tensor_view import TensorView

def prepare_norm_inputs(input_tensor, gamma_tensor, batch_size, seq_len, hidden_size):
    """Validate shapes and load normalization inputs to SBUF."""
    H0 = nl.tile_size.pmax  # 128
    BxS = batch_size * seq_len
    H1 = hidden_size // H0

    # Allocate SBUF buffers
    input_sb = nl.ndarray((H0, BxS, H1), dtype=input_tensor.dtype, buffer=nl.sbuf)
    gamma_sb = nl.ndarray((H0, H1), dtype=gamma_tensor.dtype, buffer=nl.sbuf)
    output_sb = nl.ndarray((H0, BxS, H1), dtype=input_tensor.dtype, buffer=nl.sbuf)

    input_view = TensorView(input_tensor)
    gamma_view = TensorView(gamma_tensor)
    output_view = TensorView(output_sb)

    # Validate dimensions
    BxS, H, H0, H1 = validate_shapes(input_view, gamma_view, output_view)

    # Load data to SBUF
    load_input_to_sbuf(input_view.reshape([BxS, H]), TensorView(input_sb), num_H_shards=1)
    load_gamma_to_sbuf(gamma_view, TensorView(gamma_sb), num_H_shards=1)

    return input_sb, gamma_sb, output_sb
```

### Pattern 2: Sharded normalization with LNC
```python
import nki.language as nl
from nkilib.core.utils.tensor_view import TensorView

def prepare_sharded_norm(input_tensor, gamma_tensor, hidden_size, num_shards=2):
    """Load normalization inputs with hidden-dimension sharding for LNC=2."""
    H0 = nl.tile_size.pmax  # 128
    H1 = hidden_size // H0

    input_sb = nl.ndarray((H0, BxS, H1), dtype=input_tensor.dtype, buffer=nl.sbuf)
    gamma_sb = nl.ndarray((H0, H1), dtype=gamma_tensor.dtype, buffer=nl.sbuf)

    # Load with sharding - each core loads full H for reduction
    load_input_to_sbuf(
        TensorView(input_tensor.reshape(BxS, hidden_size)),
        TensorView(input_sb),
        num_H_shards=num_shards,
        hidden_dim_tp=False,
    )
    load_gamma_to_sbuf(
        TensorView(gamma_tensor),
        TensorView(gamma_sb),
        num_H_shards=num_shards,
        hidden_dim_tp=False,
    )
    return input_sb, gamma_sb
```

## Dependencies

- `nki.isa` (`nisa`): `dma_copy`, `dma_transpose` for data movement
- `nki.language` (`nl`): `nl.tile_size.pmax` for partition dimension constant (128)
- `nkilib/core/utils/tensor_view.py`: `TensorView` class for shape manipulation (`reshape_dim`, `flatten_dims`, `expand_dim`, `permute`, `get_view`, `is_sbuf`)
- `nkilib/core/utils/kernel_assert.py`: `kernel_assert()` for shape validation

## Source

See `references/nkilib/core/subkernels/` for full implementations:
- `rmsnorm_tkg.py` — RMSNorm kernel
- `layernorm_tkg.py` — LayerNorm kernel
- `norm_tkg_utils.py` — Shared normalization utilities
- `rmsnorm_mx_quantize_tkg.py` — Fused RMSNorm + MX quantization
