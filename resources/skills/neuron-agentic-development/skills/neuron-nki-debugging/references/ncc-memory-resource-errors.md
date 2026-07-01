# NCC Memory and Resource Errors

Detailed reference for Neuron Compiler memory and resource limit errors. These errors occur when memory requirements exceed hardware limits or instruction counts are too high.

## Hardware Memory Limits

| Hardware | HBM per Device | Notes |
|----------|----------------|-------|
| Trn1 (gen2) | 32 GB | 2 NeuronCores per device |
| Trn2 (gen3) | 32 GB | Enhanced compute capabilities |
| Trn3 (gen4) | 32 GB | Latest generation |
| Inf2 (gen2) | 32 GB | Inference optimized |

## NCC_EOOM001 - Model Tensor Memory Exceeded

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The combined memory needed for the model tensors exceeds the high-bandwidth memory limit.

**Cause**: Total memory usage from I/O tensors, internal allocations, and SBUF spills exceeds available HBM.

**Memory Components**:
- **I/O tensors**: Input and output activation tensors
- **Internal allocations**: Scratchpad memory for intermediate computations
- **SBUF spills**: Data that cannot fit in on-chip SBUF memory and must spill to HBM

**Resolution**: Reduce batch/tensor size, or use pipeline/tensor parallelism via neuronx-distributed.

### Strategies

**1. Reduce Batch Size**

```python
# Before: Large batch that exceeds memory
batch_size = 256

# After: Smaller batch that fits in memory
batch_size = 64
```

**2. Use Tensor Parallelism**

```python
from neuronx_distributed.parallel_layers import ColumnParallelLinear
from neuronx_distributed import parallel_state

class ParallelSelfAttention(transformers.models.bert.modeling_bert.BertSelfAttention):
    def __init__(self, config, position_embedding_type=None):
        super().__init__(config, position_embedding_type)

        self.query = ColumnParallelLinear(
            config.hidden_size,
            self.all_head_size,
            gather_output=False
        )
        self.key = ColumnParallelLinear(
            config.hidden_size,
            self.all_head_size,
            gather_output=False
        )
        self.value = ColumnParallelLinear(
            config.hidden_size,
            self.all_head_size,
            gather_output=False
        )
        # Shard attention heads across tensor parallel ranks
        tp_size = parallel_state.get_tensor_parallel_size()
        self.num_attention_heads = self.num_attention_heads // tp_size
        self.all_head_size = self.all_head_size // tp_size
```

**3. Use Gradient Checkpointing**

Enable activation checkpointing to trade compute for memory:

```python
from neuronx_distributed.utils import checkpoint_wrapper

# Wrap model layers with checkpointing
model.encoder = checkpoint_wrapper(model.encoder)
```

---

## NCC_EOOM002 - Memory Limit Exceeded

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The combined memory needed for the model tensors exceeds the high-bandwidth memory limit.

**Cause**: Same as NCC_EOOM001. Memory usage components:
- I/O tensors
- Internal allocations
- SBUF spills

**Resolution**: Same strategies as NCC_EOOM001:
1. Reduce batch/tensor size
2. Use pipeline parallelism
3. Use tensor parallelism
4. Enable gradient checkpointing

**See also**: NCC_EOOM001 for detailed examples.

---

## NCC_EBVF030 - Instruction Count Limit (Buffer Verification)

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The number of instructions generated exceeds the limit.

**Cause**: The compiled kernel generates more instructions than the hardware can accommodate. This error occurs during buffer verification phase.

**Resolution**: Apply model parallelism to break large computational graphs into smaller subgraphs.

### Strategies

**1. Pipeline Parallelism**

Split model into stages that run on different devices:

```python
from neuronx_distributed.pipeline import NxDPPModel

# Configure pipeline parallelism
pp_config = {
    'num_stages': 4,
    'split_points': ['layer.0', 'layer.4', 'layer.8', 'layer.12']
}

model = NxDPPModel(base_model, pp_config)
```

**2. Tensor Parallelism**

Shard layers across multiple devices:

```python
from neuronx_distributed.parallel_layers import ColumnParallelLinear, RowParallelLinear

# Replace large linear layers with parallel versions
self.fc1 = ColumnParallelLinear(hidden_size, intermediate_size)
self.fc2 = RowParallelLinear(intermediate_size, hidden_size)
```

**3. Simplify Kernel**

- Reduce number of operations per kernel
- Split complex kernels into smaller pieces
- Use simpler algorithmic approaches

**See also**: NCC_EVRF007, NCC_EXTP004 (same error message and resolution)

---

## NCC_EXTP004 - Instruction Limit During Expansion

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The number of instructions generated exceeds the limit.

**Cause**: Same as NCC_EBVF030, but occurs during tensor expansion phase. The expanded kernel exceeds instruction limits.

**Resolution**: Same strategies as NCC_EBVF030:
1. Apply pipeline parallelism
2. Apply tensor parallelism
3. Simplify kernel logic

**See also**: NCC_EBVF030, NCC_EVRF007 for detailed examples.

---

## Quick Reference

| Error Code | Phase | Summary | Quick Fix |
|------------|-------|---------|-----------|
| EOOM001 | Memory allocation | Model tensors exceed HBM | Reduce batch size, use parallelism |
| EOOM002 | Memory allocation | Memory limit exceeded | Reduce batch size, use parallelism |
| EBVF030 | Buffer verification | Instruction count exceeded | Model parallelism |
| EXTP004 | Tensor expansion | Instruction count exceeded | Model parallelism |

## Common Patterns

### Memory Errors (EOOM*)

All memory errors share the same resolution strategies:

1. **Reduce data size**: Smaller batch size, shorter sequence length
2. **Pipeline parallelism**: Split model across devices sequentially
3. **Tensor parallelism**: Shard layers across devices
4. **Gradient checkpointing**: Trade compute for memory
5. **Mixed precision**: Use bf16/fp16 instead of fp32

### Instruction Limit Errors (EBVF030, EXTP004, EVRF007)

All instruction limit errors share the same resolution:

1. **Model parallelism**: Break large graphs into smaller subgraphs
2. **Simplify kernel**: Reduce operations per kernel
3. **Split kernels**: Divide complex operations into multiple smaller kernels

## Memory Estimation

Estimate memory requirements before compilation:

```python
def estimate_memory(model, batch_size, seq_len, dtype_bytes=2):
    """Estimate activation memory in GB."""
    # Rough estimate for transformer models
    hidden_size = model.config.hidden_size
    num_layers = model.config.num_hidden_layers

    # Activation memory per layer (rough estimate)
    activation_per_layer = batch_size * seq_len * hidden_size * dtype_bytes

    # Total activation memory
    total_activation = activation_per_layer * num_layers

    # Convert to GB
    return total_activation / (1024 ** 3)

# Check if model fits in HBM
estimated_gb = estimate_memory(model, batch_size=64, seq_len=512)
if estimated_gb > 32:  # HBM limit
    print(f"Warning: Estimated {estimated_gb:.1f} GB exceeds 32 GB HBM limit")
```

## Related References

- `compiler-error-codes.md` - Quick reference index for all NCC_* errors
- `ncc-verification-errors.md` - Verification errors (including EVRF007, EVRF009, EVRF024)
- `ncc-type-operation-errors.md` - Type and operation errors
