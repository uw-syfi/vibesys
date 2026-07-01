# API Translation: PyTorch/NumPy to NKI

This reference maps common PyTorch and NumPy operations to their NKI equivalents.

**Important:** All NKI ISA functions require explicit `dst` parameter as the first argument.

## Element-wise Operations

### Arithmetic

| PyTorch/NumPy | NKI | Notes |
|---------------|-----|-------|
| `a + b` | `nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.add)` | Both tensors in SBUF |
| `a - b` | `nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.subtract)` | |
| `a * b` | `nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.multiply)` | |
| `a / b` | `nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.divide)` | |
| `a + scalar` | `nisa.tensor_scalar(dst=result, data=a, op0=nl.add, operand0=value)` | |
| `a * scalar` | `nisa.tensor_scalar(dst=result, data=a, op0=nl.multiply, operand0=value)` | |

### Comparison

| PyTorch/NumPy | NKI | Notes |
|---------------|-----|-------|
| `a > b` | `nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.greater)` | Returns 0 or 1 |
| `a < b` | `nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.less)` | |
| `a >= b` | `nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.greater_equal)` | |
| `torch.maximum(a, b)` | `nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.maximum)` | |
| `torch.minimum(a, b)` | `nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.minimum)` | |

## Activation Functions

| PyTorch/NumPy | NKI | Notes |
|---------------|-----|-------|
| `torch.exp(x)` | `nisa.activation(dst=result, data=x, op=nl.exp)` | |
| `torch.relu(x)` | `nisa.activation(dst=result, data=x, op=nl.relu)` | |
| `torch.sigmoid(x)` | `nisa.activation(dst=result, data=x, op=nl.sigmoid)` | |
| `torch.tanh(x)` | `nisa.activation(dst=result, data=x, op=nl.tanh)` | |
| `F.gelu(x)` | `nisa.activation(dst=result, data=x, op=nl.gelu)` | |
| `torch.sqrt(x)` | `nisa.activation(dst=result, data=x, op=nl.sqrt)` | |
| `torch.rsqrt(x)` | `nisa.activation(dst=result, data=x, op=nl.rsqrt)` | 1/sqrt(x) |
| `1/x` | `nisa.reciprocal(dst=result, data=x)` | |
| `-x` | `nisa.tensor_scalar(dst=result, data=x, op0=nl.multiply, operand0=-1.0)` | |

## Reduction Operations

| PyTorch/NumPy | NKI | Notes |
|---------------|-----|-------|
| `torch.sum(x, dim=axis)` | `nisa.tensor_reduce(dst=result, data=x, op=nl.add, axis=axis)` | |
| `torch.max(x, dim=axis)` | `nisa.tensor_reduce(dst=result, data=x, op=nl.maximum, axis=axis)` | |
| `torch.min(x, dim=axis)` | `nisa.tensor_reduce(dst=result, data=x, op=nl.minimum, axis=axis)` | |
| `torch.mean(x, dim=axis)` | Sum then divide by count | No direct mean op |

### Reduction Example (Sum over axis=1)

```python
# PyTorch: result = torch.sum(x, dim=1, keepdim=True)
# Shape: x is (P, F), result is (P, 1)

# NKI:
result = nl.ndarray((P, 1), dtype=x.dtype, buffer=nl.sbuf)
nisa.tensor_reduce(dst=result, data=x, op=nl.add, axis=1)
```

## Matrix Operations

| PyTorch/NumPy | NKI | Notes |
|---------------|-----|-------|
| `a @ b` | `nisa.nc_matmul(dst=psum_result, stationary=a, moving=b)` | Result in PSUM |
| `a.T` | `nisa.nc_transpose(dst=result, data=a)` | |

### Matrix Multiply Pattern

```python
# PyTorch: c = a @ b  where a: (M, K), b: (K, N)
# Constraint: K <= 2048, result free dim <= 512 (gen2/3) or 4096 (gen4)

# NKI:
psum_result = nl.ndarray((M, N), dtype=nl.float32, buffer=nl.psum)
nisa.nc_matmul(dst=psum_result, stationary=a_sbuf, moving=b_sbuf)

# Copy from PSUM to SBUF for further operations
sbuf_result = nl.ndarray((M, N), dtype=output_dtype, buffer=nl.sbuf)
nisa.tensor_copy(dst=sbuf_result, src=psum_result)
```

## Data Type Mapping

| PyTorch | NKI | Notes |
|---------|-----|-------|
| `torch.float32` | `nl.float32` | Full precision |
| `torch.float16` | `nl.float16` | Half precision |
| `torch.bfloat16` | `nl.bfloat16` | Brain float |
| `torch.int32` | `nl.int32` | Integer |
| `torch.int8` | `nl.int8` | Quantized |
| `torch.float8_e4m3fn` | `nl.float8_e4m3` | FP8 (gen3+ only) |
| `torch.float8_e5m2` | `nl.float8_e5m2` | FP8 (gen3+ only) |

## Memory Operations

| Operation | NKI | Notes |
|-----------|-----|-------|
| Load from HBM | `nisa.dma_copy(dst=sbuf_tile, src=hbm_tensor[slice])` | |
| Store to HBM | `nisa.dma_copy(dst=hbm_tensor[slice], src=sbuf_tile)` | |
| Copy SBUF to SBUF | `nisa.tensor_copy(dst=dest, src=src)` | |
| Copy PSUM to SBUF | `nisa.tensor_copy(dst=sbuf, src=psum)` | Type conversion |
| Initialize to value | `nisa.memset(dst=tensor, value=0.0)` | |

## Shape Operations

| PyTorch/NumPy | NKI | Notes |
|---------------|-----|-------|
| `x.reshape(shape)` | `x.reshape(shape)` | Zero-copy reshape |
| `x.view(shape)` | `x.reshape(shape)` | Same as reshape |
| Broadcasting | Manual expansion | Explicit broadcast required |

## Not Directly Supported

These operations require manual implementation:

- `torch.softmax()` - Implement as: exp(x - max(x)) / sum(exp(x - max(x)))
- `torch.layer_norm()` - Implement as: (x - mean) / sqrt(var + eps) * gamma + beta
- `torch.gather()` - Use dynamic access patterns (use `/neuron-nki-docs dynamic access` for details)
- `torch.scatter()` - Use dynamic access patterns

## Need More APIs?

Use `/neuron-nki-docs <query>` for:
- Complete API signatures and parameters
- APIs not listed here
- Hardware generation support details
