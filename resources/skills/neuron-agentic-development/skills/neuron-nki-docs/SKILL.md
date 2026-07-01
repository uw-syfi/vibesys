---
name: neuron-nki-docs
description: |
  Research NKI documentation for API lookups, tutorials, error codes, architecture,
  and optimization guides. Use when user asks "what does <API> do", "how to do <task>",
  "what is error <code>", "NKI <API> signature", "find NKI tutorial for <topic>",
  "look up <symbol>", or needs any NKI documentation reference.
argument-hint: "[query or API name]"
context: fork
---

Research $ARGUMENTS thoroughly:

1. Find relevant files through provided indices using Glob and Grep
2. Read and analyze document and code
3. Summarize findings with specific file references

# NKI Documentation Lookup

This skill provides comprehensive access to NKI (Neuron Kernel Interface) documentation for AWS Trainium/Inferentia kernel development. Use this skill to answer questions about NKI APIs, tutorials, hardware architecture, error codes, and optimization techniques.

## Query Type Routing

Route queries to the appropriate documentation based on the query pattern:

| Query Pattern | Start With | Then Read |
|---------------|------------|-----------|
| `nl.*` or `nisa.*` or API name | `references/indices/symbol-lookup.md` | Linked API doc |
| "how to [task]" | `references/indices/task-routing.md` | Linked tutorial |
| "NCC_*" or error code | `references/debugging/error-codes-index.md` | Specific error doc |
| gen2/gen3/gen4/trn1/trn2/trn3 | `references/architecture/` | Specific arch doc |
| optimize/profile/performance | `references/optimization/` | Relevant guide |
| browse/list all docs | `references/indices/hierarchical-toc.md` | Navigate tree |

## Search Strategy

1. **Start with indices** - Use the index files in `references/indices/` to find the exact documentation needed
2. **Follow links** - Read the linked documentation file for full details
3. **Cross-reference** - For complex topics, combine information from multiple sources

### Index Files

- `indices/symbol-lookup.md` - Alphabetical API function index with direct links
- `indices/task-routing.md` - Task-oriented guide ("I want to...")
- `indices/hierarchical-toc.md` - Complete documentation tree structure

## Directory Guide

| Directory | Contents |
|-----------|----------|
| `references/architecture/` | Hardware architecture guides for Trainium/Inferentia generations |
| `references/debugging/` | Error codes index and individual error documentation |
| `references/downloads/` | Valid Python kernel examples (deprecated patterns excluded) |
| `references/indices/` | Navigation aids and lookup tables |
| `references/optimization/` | Performance tuning, profiling, migration guides |
| `references/programming/` | Core NKI concepts, tutorials, and API reference |
| `references/programming/api/` | Detailed API documentation by category |
| `references/programming/tutorials/` | Step-by-step kernel implementation tutorials |
| `references/reference/` | FAQ and release notes |

## Quick API Reference

### Most Common APIs

**Data Movement:**
- `nki.isa.dma_copy` - Load/store data between HBM and SBUF
- `nki.isa.dma_transpose` - Transpose during DMA transfer

**Tensor Operations:**
- `nki.isa.nc_matmul` - Matrix multiplication on Tensor Engine
- `nki.isa.tensor_tensor` - Element-wise operations
- `nki.isa.tensor_scalar` - Broadcast scalar operations
- `nki.isa.tensor_reduce` - Reduction along axes

**Memory Allocation:**
- `nl.ndarray` - Create tensor in SBUF
- `nl.zeros` - Create zero-initialized tensor

**Loop Constructs:**
- `range` - Standard loop iterator (recommended)
- `nl.affine_range` / `nl.sequential_range` / `nl.static_range` - Legacy aliases for `range` (all have identical effect in NKI 0.3.0+)

**SPMD:**
- `nl.program_id` - Get current program index
- `nl.num_programs` - Get total program count

### Module Aliases

The documentation uses these standard aliases:
```python
import nki
import nki.language as nl
import nki.isa as nisa
```

### Activation Function Operators

**Important:** Symbols like `nl.exp`, `nl.sigmoid`, `nl.tanh`, etc. are **op specifiers**, not standalone callable functions. They must be passed as the `op` parameter to `nisa.activation()`:

```python
# CORRECT: Use as op parameter
result = nisa.activation(op=nl.exp, data=tensor, scale=scale_tensor, bias=bias_tensor)

# INCORRECT: These are NOT callable functions
# result = nl.exp(tensor)  # This does NOT work
```

## Hardware Constraints

Critical limits to remember when answering questions:

| Constraint | Limit | Notes |
|------------|-------|-------|
| Partition dimension (P) | ≤ 128 | First axis of on-chip tensors |
| PSUM free dimension (F) | ≤ 512 (gen2/3) / ≤ 4096 (gen4) | Second axis in PSUM buffer |
| SBUF free dimension (F) | ≤ 32767 | Second axis in SBUF buffer |
| MatMul K dimension | ≤ 2048 | Contraction dimension per tile |

### Hardware Generations

| Generation | Devices | Key Features |
|------------|---------|--------------|
| gen2 (v2) | Trn1, Inf2 | Baseline NKI support |
| gen3 (v3) | Trn2 | FP8 support, Double FP8 mode |
| gen4 (v4) | Trn3 | MXFP8/MXFP4, Quad-MX mode |

## Source NKI Kernel

The Python example files in `references/downloads/` use the latest NKI API patterns and can be used as reference implementations:

- `average_pool2d_nki_kernels.py` — 2D average pooling kernel
- `matrix_multiplication_nki_kernels.py` — Tiled matrix multiplication kernel
- `transpose2d_nki_kernels.py` — 2D transpose kernel
- `mamba_nki_kernels.py` — Fused Mamba state-space model kernel
- `test_nki_isa_local_gather.py` — Local gather ISA test

> **Note:** The `fused_mamba` tutorial (`references/programming/tutorials/fused_mamba.md`) still uses Beta 1 syntax (`nl.load`/`nl.store`). The optimization concepts (loop reordering, data reuse, tiling for temporal locality) are valid, but do NOT copy its code patterns — use `nisa.dma_copy` from `/neuron-nki-writing` for any code generation.

## Common Query Examples

### API Lookup
Query: "What is nisa.nc_matmul?"
→ Read `indices/symbol-lookup.md` → Find link → Read `programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul`

### Error Code
Query: "What does NCC_EVRF001 mean?"
→ Read `debugging/error-codes-index.md` → Read `debugging/error-codes/EVRF001.md`

### Tutorial
Query: "How do I implement matrix multiplication?"
→ Read `indices/task-routing.md` → Read `programming/tutorials/matrix_multiplication.md`

### Architecture
Query: "What's different in Trainium3?"
→ Read `architecture/trainium3_arch.md`

### Optimization
Query: "How do I profile my kernel?"
→ Read `optimization/use-neuron-profile.md`

## Response Guidelines

When answering NKI documentation queries:

1. **Cite sources** - Reference the specific documentation file used
2. **Include signatures** - For API queries, include function signatures and key parameters
3. **Note hardware requirements** - Mention if APIs are generation-specific (v2/v3/v4)
4. **Link related docs** - Point to related tutorials or guides for context
5. **Warn about deprecated patterns** - If documentation shows old patterns, note this

## Error Code Format

NKI compiler errors follow the pattern `NCC_<category><number>`:
- `EOOM*` - Out of memory errors
- `EVRF*` - Verification/validation errors
- `EUOC*` - Unsupported operation errors
- `EBVF*` - Backend verification failures
- `ESPP*` - Shape/type errors

The full index is in `debugging/error-codes-index.md` with individual files in `debugging/error-codes/`.

## Related Skills

| Skill | Purpose |
|-------|---------|
| `/neuron-nki-writing` | Write NKI kernels from specifications |
| `/neuron-nki-debugging` | Debug compiler errors on device |
| `/neuron-nki-profiling` | Profile kernel performance |
| `/neuron-nki-profile-querying` | Query and analyze kernel profile data |
