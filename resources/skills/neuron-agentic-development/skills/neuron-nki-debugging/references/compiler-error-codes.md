# Neuron Compiler Error Codes (NCC_*)

Quick reference index to Neuron Compiler (neuronx-cc) error codes. For detailed fixes and code examples, see the linked reference files.

## Overview

Neuron Compiler error codes (NCC_* prefix) indicate issues during NEFF generation from NKI kernels. These errors occur during the compilation phase after NKI kernel code has been successfully parsed but before executable NEFF files can be generated.

**Error Code Format**: `NCC_<CATEGORY><NUMBER>`
- Category: 4-letter code indicating error type
- Number: 3-digit identifier within category

**Total Error Codes**: 28 documented codes across 10 categories

## Error Categories

### NCC_EVRF - Verification Errors (17 codes)

Unsupported operations, data types, or configurations detected during compilation verification.

**Detailed reference**: `ncc-verification-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_EVRF001 | An unsupported operator was used | Use alternative operator from `neuronx-cc list-operators` |
| NCC_EVRF004 | Complex data types are not supported | Use real-valued tensors or emulate complex arithmetic |
| NCC_EVRF005 | Unsupported F8E4M3FNUZ/F8E5M2FNUZ data type | Convert to float16/bfloat16 |
| NCC_EVRF006 | Unsupported RNG algorithm | Use default RNG via standard APIs |
| NCC_EVRF007 | Instruction count exceeds limit | Apply model parallelism |
| NCC_EVRF009 | Activation memory exceeds HBM limit | Reduce batch size or use parallelism |
| NCC_EVRF010 | Simultaneous input and kernel dilation | Use only input OR kernel dilation |
| NCC_EVRF011 | Strided convolution with dilated input | Remove stride or input dilation |
| NCC_EVRF013 | TopK does not support int32/int64 | Cast to float before TopK |
| NCC_EVRF015 | Unrecognized custom call target | Use supported custom call target |
| NCC_EVRF016 | Scatter-reduce with integer/boolean types | Cast to float types |
| NCC_EVRF017 | Reduce-window with base dilation > 1 | Set base_dilation to (1,1,1,1) |
| NCC_EVRF018 | Reduce-window with window dilation > 1 | Set window_dilation to (1,1,1,1) |
| NCC_EVRF019 | Reduce-window wrong operand count | Split into single-operand operations |
| NCC_EVRF022 | Shift-right-arithmetic on non-32-bit | Cast first argument to 32-bit |
| NCC_EVRF024 | Output tensor size exceeds 4GB | Reduce tensor size or use parallelism |
| NCC_EVRF031 | Scatter out-of-bounds (iota size mismatch) | Match iota size to operand dimension |

### NCC_EOOM - Out of Memory Errors (2 codes)

Memory requirements exceed hardware limits.

**Detailed reference**: `ncc-memory-resource-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_EOOM001 | Model tensor memory exceeds HBM limit | Reduce batch size, use tensor/pipeline parallelism |
| NCC_EOOM002 | Memory limit exceeded | Reduce batch size, use tensor/pipeline parallelism |

**Hardware HBM Limits**:
- Trn1/Trn2/Trn3: 32 GB per device
- Inf2: 32 GB per device

### NCC_EARG - Argument Validation Errors (1 code)

**Detailed reference**: `ncc-type-operation-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_EARG001 | Unsupported LNC configuration | Use supported LNC count for target hardware |

**Supported LNC Configurations by Hardware**:

| Hardware | Supported LNC Values |
|----------|---------------------|
| Trn1 (gen2) | 1 |
| Inf2 (gen2) | 1, 2 |
| Trn2 (gen3) | 1, 2, 4 |
| Trn3 (gen4) | 1, 2, 4, 8 |

### NCC_EBVF - Buffer Verification Errors (1 code)

**Detailed reference**: `ncc-memory-resource-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_EBVF030 | Instruction count exceeds limit | Apply model parallelism |

### NCC_EHCA - Custom Call Errors (1 code)

**Detailed reference**: `ncc-type-operation-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_EHCA005 | Unrecognized custom call target | Use supported custom call target |

### NCC_ESFH - Safe Float Handling Errors (1 code)

**Detailed reference**: `ncc-type-operation-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_ESFH002 | 64-bit constant cannot convert to 32-bit | Use 32-bit constants |

### NCC_ESPP - Shape Parser Errors (2 codes)

Data type support and shape parsing issues.

**Detailed reference**: `ncc-type-operation-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_ESPP004 | Unsupported data type for codegen | Use fp32/fp16/bf16 |
| NCC_ESPP047 | Unsupported FP8 data type | Convert to float16 or use gen3+ hardware |

**Supported Dtypes by Hardware**:

| Dtype | gen2 (Trn1/Inf2) | gen3+ (Trn2/Trn3) |
|-------|------------------|-------------------|
| fp32, fp16, bf16 | Yes | Yes |
| fp8_e4m3, fp8_e5m2 | No | Yes |

### NCC_EUOC - Unsupported Operation Errors (1 code)

**Detailed reference**: `ncc-type-operation-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_EUOC002 | Unsupported operator | Use alternative operator |

### NCC_EXSP - Expansion Errors (1 code)

**Detailed reference**: `ncc-type-operation-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_EXSP001 | Activation memory exceeds HBM limit | Reduce size, use parallelism |

### NCC_EXTP - Expansion Tensor Errors (1 code)

**Detailed reference**: `ncc-memory-resource-errors.md`

| Error Code | Error Message | Quick Fix |
|------------|---------------|-----------|
| NCC_EXTP004 | Instruction count exceeds limit | Apply model parallelism |

## Hardware Compatibility

All error codes apply to:
- **Inf1**: Inferentia 1
- **Inf2**: Inferentia 2
- **Trn1**: Trainium 1
- **Trn2**: Trainium 2
- **Trn3**: Trainium 3

Check individual error code documentation for hardware-specific notes.

## Quick Lookup

### By Symptom

| Symptom | Likely Error Code | Quick Fix |
|---------|-------------------|-----------|
| Unsupported operator | NCC_EVRF001, NCC_EUOC002 | Find alternative operator |
| Out of memory | NCC_EOOM001, NCC_EOOM002, NCC_EXSP001 | Reduce batch size, use parallelism |
| FP8 type error | NCC_EVRF005, NCC_ESPP047 | Check hardware generation, convert type |
| Instruction count too high | NCC_EVRF007, NCC_EBVF030, NCC_EXTP004 | Apply model parallelism |
| Convolution dilation error | NCC_EVRF010, NCC_EVRF011 | Use single dilation type |
| Data type not supported | NCC_EVRF004, NCC_ESPP004 | Use supported dtype |
| Tensor size exceeds limit | NCC_EVRF024 | Reduce tensor dimensions |
| Custom call error | NCC_EVRF015, NCC_EHCA005 | Use supported target name |
| LNC configuration error | NCC_EARG001 | Use supported LNC for hardware |

### By Operation Type

| Operation | Common Errors | Notes |
|-----------|---------------|-------|
| Matrix operations | NCC_EVRF001, NCC_EUOC002 | Some ops not supported |
| Convolutions | NCC_EVRF010, NCC_EVRF011 | Dilation restrictions |
| Reductions | NCC_EVRF017, NCC_EVRF018 | Window dilation limits |
| Scatter/Gather | NCC_EVRF016, NCC_EVRF031 | Type and bounds checks |
| Type casting | NCC_EVRF004, NCC_EVRF005 | Limited dtype support |
| Random number generation | NCC_EVRF006 | Algorithm restrictions |

## Error Resolution Workflow

```
NCC_* Error Encountered
        │
        ▼
┌─────────────────────────┐
│ 1. Identify error code  │
│    (NCC_XXXX###)        │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ 2. Look up category in  │
│    this index           │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ 3. Read detailed docs   │
│    (linked reference)   │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ 4. Apply fix from code  │
│    examples             │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ 5. Verify compilation   │
│    succeeds             │
└─────────────────────────┘
```

## Compilation Stages

```
NKI Kernel Code
      │
      ▼
  NKI Compiler
      │
      ▼
  KLIR (IR)
      │
      ▼
neuronx-cc       ← NCC_* errors occur here
      │
      ▼
   NEFF File
```

NCC_* errors occur during the neuronx-cc phase when generating NEFF files from the intermediate representation.

## Best Practices

**When encountering NCC_* errors**:

1. **Read the full error message** - Contains context and file/line info
2. **Check detailed reference** - See linked files for code examples
3. **Verify hardware compatibility** - Some features are generation-specific
4. **Look for alternative approaches** - Many operations have multiple implementations
5. **Simplify if complex** - Break large kernels into smaller pieces

**Prevention**:
- Use supported operations for target hardware
- Check dtype compatibility before compilation
- Monitor memory usage for large models
- Test with small inputs before full-scale device compilation

## Related References

- `../SKILL.md` - Main NKI debugging skill
- `ncc-verification-errors.md` - Detailed EVRF error documentation
- `ncc-memory-resource-errors.md` - Detailed memory/resource error documentation
- `ncc-type-operation-errors.md` - Detailed type/operation error documentation
- NKI documentation error codes index (bundled in `/neuron-nki-docs` skill references)
