---
name: neuron-nki-profiling
description: |
  This skill guides using the cli to generate NKI kernel profiles (NEFF + NTFF pairs) to analyze performance on Neuron hardware.
  Use when encountering "profile kernel", "capture execution trace",
  "generate NEFF", "get summary-json",
  or asking "how to profile NKI kernel".
argument-hint: "[kernel file]"
---

# Profiling NKI Kernels

This skill provides a complete workflow for profiling NKI kernel execution on Trainium/Inferentia hardware using Neuron profiling tools.

## Quick Start

Minimal workflow to profile a kernel:

```bash
# 1. Set environment variables in Python before kernel execution
os.environ['NEURON_RT_INSPECT_ENABLE'] = '1'
os.environ['NEURON_RT_INSPECT_DEVICE_PROFILE'] = '1'
os.environ['NEURON_RT_INSPECT_OUTPUT_DIR'] = './output'

# 2. Run kernel to generate NEFF
python my_kernel.py

# 3. Find the NKI kernel NEFF (skip XLA-generated NEFFs)
NEFF_PATH=$(python3 scripts/identify-neffs.py ./output my_kernel_func_name)

# 4. Capture profile with neuron-explorer
neuron-explorer capture -n $NEFF_PATH -s profile.ntff --profile-nth-exec=2 --enable-dge-notifs

# 5. View results with neuron-explorer
neuron-explorer view --output-format summary-json -n $NEFF_PATH -s profile.ntff
```

The workflow generates two key artifacts:
- **NEFF file**: Compiled kernel binary, generated during execution
- **NTFF file**: Execution trace captured by neuron-explorer

## Prerequisites

Before profiling kernels, resolve the NKI virtual environment path:

1. Check environment: `echo $NKI_VENV_PATH`
2. If empty, read `.claude/nki-dev-suite.local.md` and extract `nki_venv_path` from YAML frontmatter
3. If still not found, report: "NKI_VENV_PATH not configured. Set the environment variable or create .claude/nki-dev-suite.local.md with nki_venv_path in frontmatter."

Activate before running any profiling commands:
```bash
source $NKI_VENV_PATH/bin/activate
```

**Hardware requirement:** Profiling requires execution on actual Trainium/Inferentia hardware.

## Complete Profiling Workflow

### Step 1: Set Environment Variables

Add these environment variables in your Python script before kernel execution:

```python
import os

# Enable runtime inspection and device profiling
os.environ['NEURON_RT_INSPECT_ENABLE'] = '1'
os.environ['NEURON_RT_INSPECT_DEVICE_PROFILE'] = '1'
os.environ['NEURON_RT_INSPECT_OUTPUT_DIR'] = './output'

# Compiler flags for target hardware
os.environ['NEURON_CC_FLAGS'] = '--target trn2 --lnc 1' # use lnc=2 if explicitely told to.  

# Pin to a specific neuron core(s) to avoid conflicts with concurrent sessions
os.environ['NEURON_RT_VISIBLE_CORES'] = '0' # '0,1', '0-1'
```

| Environment Variable | Description |
|---------------------|-------------|
| `NEURON_RT_INSPECT_ENABLE` | Enable runtime inspection |
| `NEURON_RT_INSPECT_DEVICE_PROFILE` | Enable device-level profiling |
| `NEURON_RT_INSPECT_OUTPUT_DIR` | Directory for NEFF output |
| `NEURON_RT_VISIBLE_CORES` | Pin to specific core(s) — prevents contention when multiple agents profile concurrently |

### Step 2: Execute Kernel

Run your kernel script. This compiles and executes the kernel, generating the NEFF file in the output directory.

```bash
python my_kernel.py
```

**Important: Compute references on CPU.** If the test script computes a reference result (e.g., `torch.matmul` for comparison), do it on CPU — not on the XLA device. Every XLA graph compiled on-device generates a separate NEFF. Running reference operations on-device creates extra NEFFs that make it hard to identify the NKI kernel's NEFF.

```python
# CORRECT: Reference on CPU — generates only the NKI kernel NEFF
expected = torch.relu(torch.matmul(lhs.cpu(), rhs.cpu()))
result = my_nki_kernel(lhs, rhs)  # Only this generates a NEFF

# WRONG: Reference on device — generates an extra NEFF
expected = torch.relu(torch.matmul(lhs, rhs))  # Compiles to its own NEFF!
result = my_nki_kernel(lhs, rhs)                # Another NEFF
```

The runtime creates a subdirectory with instance and process ID naming:
```
./output/
└── i-0823210096b01e7ec_pid_1187583/
    └── neff_*_vnc_0.neff
```

### Step 3: Create Dedicated Profile Folder

Create a dedicated folder for this profiling iteration. This prevents confusion when comparing multiple optimization attempts:

```bash
mkdir -p ./profiles/run_001
```

Organize profile iterations:
```
./profiles/
├── run_001/              # Baseline profiling
│   ├── profile.ntff
│   └── metrics.json
├── run_002/              # After first optimization
│   ├── profile.ntff
│   └── metrics.json
└── run_003/              # After second optimization
```

### Step 4: Capture Profile with neuron-explorer

Locate the NKI kernel NEFF and capture execution profile:

```bash
# Find NKI kernel NEFF by function name (skips XLA-generated NEFFs)
NEFF_PATH=$(python3 scripts/identify-neffs.py ./output my_kernel_func_name)

# Capture profile trace
neuron-explorer capture \
    -n $NEFF_PATH \
    -s ./profiles/run_001/profile.ntff \
    --profile-nth-exec=2 \
    --enable-dge-notifs
```

| Flag | Description |
|------|-------------|
| `-n` | Path to NEFF file |
| `-s` | Output path for NTFF trace file |
| `--profile-nth-exec=2` | Profile the 2nd execution (skip warmup) |
| `--enable-dge-notifs` | Enable DMA engine notifications for detailed analysis |

### Step 5: View Results with neuron-explorer (JSON)

Generate a JSON summary of the profile results:

```bash
neuron-explorer view \
    --output-format summary-json \
    -n $NEFF_PATH \
    -s ./profiles/run_001/profile.ntff
```

This outputs structured JSON with all metrics. Parse for specific values:

```bash
neuron-explorer view \
    --output-format summary-json \
    -n $NEFF_PATH \
    -s ./profiles/run_001/profile.ntff | jq '.latency'
```

**Common JSON queries:**

```bash
# Get latency in milliseconds
jq '.latency'

# Get all engine utilizations
jq '{tensor: .tensor_engine_active_time_percent, vector: .vector_engine_active_time_percent}'

# Get memory metrics
jq '{hbm_read: .hbm_read_bytes, hbm_write: .hbm_write_bytes}'

# Check if memory-bound or compute-bound
jq '{intensity: .mm_arithmetic_intensity, peak_ratio: .peak_flops_bandwidth_ratio}'
```

Save the full JSON output for later comparison:

```bash
neuron-explorer view \
    --output-format summary-json \
    -n $NEFF_PATH \
    -s ./profiles/run_001/profile.ntff > ./profiles/run_001/metrics.json
```

### Step 6: Querying the profile and/or profile analysis (optional)

For detailed analysis of the kernel profile, use the /neuron-nki-profile-querying skill. It allows for high level performance bounds analysis, as well as zoomed in, instruction level
investigation of specific inefficiencies through python on parquet. 

## Output Directory Structure

Understanding the generated file structure:

```
./output/                                    # NEURON_RT_INSPECT_OUTPUT_DIR
└── i-0823210096b01e7ec_pid_1187583/        # Instance ID + process ID
    ├── neff_307444798579300_vnc_0.neff     # One NEFF per compiled XLA graph
    ├── neff_324387526933418_vnc_0.neff     # (may include non-NKI NEFFs)
    ├── 307444798579300_vnc_0.ntff          # Matching execution traces
    ├── 324387526933418_vnc_0.ntff
    └── ntrace.pb                           # System trace metadata

./profiles/                                  # Organized profile iterations
├── run_001/
│   ├── profile.ntff                        # Execution trace
│   └── metrics.json                        # Summary metrics
├── run_002/
│   └── ...
```

The instance/pid subdirectory naming (`i-xxx_pid_xxx`) is automatic and includes the EC2 instance ID and process ID for traceability.

## NEFF Identification

When the output directory contains multiple NEFFs (from multiple on-device operations), use the included `identify-neffs.py` script to identify which NEFF belongs to which kernel:

```bash
# List all NEFFs with identification
python3 scripts/identify-neffs.py ./output/i-*_pid_*/
# Output:
#   [NKI:matmul_relu] ./output/.../neff_389250674131083_vnc_0.neff
#     inputs: ['lhs_T', 'rhs', 'tmp.4']  outputs: ['output.51']
#     ntff: ./output/.../389250674131083_vnc_0.ntff
#   [XLA:broadcast,dot,maximum] ./output/.../neff_418708643727628_vnc_0.neff

# Find a specific kernel by name (useful with multiple NKI kernels)
NEFF_PATH=$(python3 scripts/identify-neffs.py ./output/i-*_pid_*/ matmul_relu)
```

**How it works:** Each NEFF embeds its compile workdir path (`/tmp/.../neuroncc_compile_workdir/<uuid>/`). The script reads the HLO module in that workdir. NKI kernels appear as `custom-call` ops with `AwsNeuronCustomNativeKernel` and carry a base64-encoded JSON blob containing `func_name`, `input_names`, and `output_names`.

**Limitation:** Depends on compile workdirs in `/tmp/` still existing. Run promptly after kernel execution.

## Key Metrics Quick Reference

| Metric | Description | Target |
|--------|-------------|--------|
| `latency` | Total kernel execution time (ms) | Lower is better |
| `tensor_engine_active_time_percent` | TensorE utilization | >90% for compute-bound |
| `hbm_read_bytes` | HBM read traffic | Minimize |
| `hbm_write_bytes` | HBM write traffic | Minimize |
| `mm_arithmetic_intensity` | FLOPs per byte of memory traffic | Compare to peak ratio |

## Comparing Optimization Iterations

When optimizing a kernel, compare metrics across iterations:

```bash
# Baseline measurement
neuron-explorer view --output-format summary-json \
    -n $NEFF -s ./profiles/baseline/profile.ntff > ./profiles/baseline/metrics.json

# After optimization
neuron-explorer view --output-format summary-json \
    -n $NEFF -s ./profiles/optimized/profile.ntff > ./profiles/optimized/metrics.json

# Compare latencies
echo "Baseline: $(jq .latency ./profiles/baseline/metrics.json)"
echo "Optimized: $(jq .latency ./profiles/optimized/metrics.json)"
```

**Optimization tracking table:**

| Iteration | Change | Latency (ms) | TensorE (%) |
|-----------|--------|--------------|-------------|
| Baseline | - | 1.23 | 45% |
| Larger tiles | Increased tile 64→128 | 0.95 | 72% |
| Double buffer | Added prefetching | 0.78 | 89% |

Keep notes on what changed between iterations to correlate optimizations with metric improvements.

## Complete Example

See `examples/basic-profiling-workflow.py` for a complete end-to-end profiling script demonstrating all steps: environment setup, kernel execution, NEFF identification, profile capture, and JSON metric extraction.

## Configuration

**Required settings:**

| Setting | Source | Description |
|---------|--------|-------------|
| `nki_venv_path` | `.claude/nki-dev-suite.local.md` or `NKI_VENV_PATH` | Python venv with neuronx packages |

**Environment variables (set in kernel script):**

| Variable | Value | Purpose |
|----------|-------|---------|
| `NEURON_RT_INSPECT_ENABLE` | `1` | Enable runtime inspection |
| `NEURON_RT_INSPECT_DEVICE_PROFILE` | `1` | Enable device profiling |
| `NEURON_RT_INSPECT_OUTPUT_DIR` | Path | NEFF output directory |

## Related Skills

| Skill | Purpose |
|-------|---------|
| `/neuron-nki-profile-querying` | Detailed profile querying and analysis |
| `/neuron-nki-debugging` | Debug compilation errors |
| `/neuron-nki-docs` | Look up API documentation |
| `/neuron-nki-writing` | Write NKI kernels |

## Troubleshooting

**Multiple NEFFs generated (can't tell which is the NKI kernel):**
- **Primary fix**: Compute reference operations (e.g., `torch.matmul`) on CPU, not on the XLA device. Each on-device XLA graph generates its own NEFF.
- **Identify NEFFs**: Use `python3 scripts/identify-neffs.py ./output` to list all NEFFs with their type (NKI vs XLA) and kernel names. See the [NEFF Identification](#neff-identification) section for details.
- **Match NEFF to NTFF**: Each NEFF `neff_<ID>_vnc_0.neff` has a matching trace `<ID>_vnc_0.ntff` in the same directory.

**No NEFF file generated:**
- Verify `NEURON_RT_INSPECT_ENABLE=1` is set before imports
- Check `NEURON_RT_INSPECT_OUTPUT_DIR` path exists and is writable
- Ensure kernel actually executed (print forces XLA compilation)
- Confirm you are on Trainium/Inferentia hardware: `neuron-ls`

**neuron-explorer capture fails:**
- Verify running on Trainium/Inferentia hardware
- Check NEFF file path is correct with `ls -la <path>`
- Ensure neuronx packages are installed in venv
- Check sufficient disk space for NTFF file

**Empty or minimal profile data:**
- Use `--profile-nth-exec=2` to skip warmup execution
- Add `--enable-dge-notifs` for detailed DMA analysis
- Verify kernel ran successfully before profiling
- Check NTFF file size is non-trivial: `ls -lh profile.ntff`

**Latency varies between runs:**
- Use `--profile-nth-exec=2` or higher to skip warmup
- Ensure system is not under other load
- Run multiple iterations and average results
- Check for thermal throttling in profile output
