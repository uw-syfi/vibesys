"""
Basic NKI Kernel Profiling Workflow

This example demonstrates the complete workflow for profiling an NKI kernel:
1. Set profiling environment variables
2. Execute kernel (generates NEFF)
3. Create dedicated profile folder
4. Capture profile with neuron-explorer
5. View results with neuron-explorer

Run this script on Trainium/Inferentia hardware with the NKI venv activated.
"""

import os
import subprocess
from pathlib import Path
from datetime import datetime

# =============================================================================
# Step 1: Set profiling environment variables BEFORE any neuronx imports
# =============================================================================

os.environ['NEURON_RT_INSPECT_ENABLE'] = '1'
os.environ['NEURON_RT_INSPECT_DEVICE_PROFILE'] = '1'
os.environ['NEURON_RT_INSPECT_OUTPUT_DIR'] = './output'
os.environ['NEURON_CC_FLAGS'] = '--target trn2 --lnc 1'

# Now import neuronx packages
import torch
from torch_xla.core import xla_model as xm
import nki
import nki.language as nl
import nki.isa as nisa


# =============================================================================
# Define kernel to profile
# =============================================================================

def kernel_assert(condition, message=""):
    """Validate kernel preconditions (raises at trace time)."""
    assert condition, message


os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

@nki.jit
def add_kernel(a_input, b_input):
    """
    Element-wise addition kernel.

    Args:
        a_input: First input tensor in HBM
        b_input: Second input tensor in HBM (same shape as a_input)

    Returns:
        Output tensor with element-wise sum
    """
    # Validate inputs
    kernel_assert(a_input.shape == b_input.shape, "Input shapes must match")
    kernel_assert(a_input.shape[0] <= nl.tile_size.pmax, "First dimension exceeds tile size")

    # Load inputs from HBM to SBUF
    a_tile = nl.ndarray(a_input.shape, dtype=a_input.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=a_tile, src=a_input)

    b_tile = nl.ndarray(b_input.shape, dtype=b_input.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=b_tile, src=b_input)

    # Compute element-wise addition
    c_tile = nl.ndarray(a_input.shape, dtype=a_input.dtype, buffer=nl.sbuf)
    nisa.tensor_tensor(dst=c_tile, data1=a_tile, data2=b_tile, op=nl.add)

    # Store result back to HBM
    c_output = nl.ndarray(a_input.shape, dtype=a_input.dtype, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=c_output, src=c_tile)

    return c_output


# =============================================================================
# Step 2: Execute kernel (generates NEFF in ./output/i-xxx_pid_xxx/)
# =============================================================================

print("Step 2: Executing kernel...")
device = xm.xla_device()

a = torch.ones((64, 128), dtype=torch.float16).to(device=device)
b = torch.ones((64, 128), dtype=torch.float16).to(device=device)

c = add_kernel(a, b)
print(c)  # Forces XLA compilation and execution

print(f"Kernel executed. NEFF files generated in: {os.environ['NEURON_RT_INSPECT_OUTPUT_DIR']}")


# =============================================================================
# Step 3: Create dedicated profile folder for this iteration
# =============================================================================

profile_dir = Path(f"./profiles/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
profile_dir.mkdir(parents=True, exist_ok=True)
print(f"Step 3: Created profile directory: {profile_dir}")


# =============================================================================
# Step 4: Find NEFF and prepare capture command
# =============================================================================

output_dir = Path('./output')
neff_files = list(output_dir.glob('*/*.neff'))

if not neff_files:
    print("ERROR: No NEFF files found in output directory")
    exit(1)

neff_path = neff_files[0]
ntff_path = profile_dir / 'profile.ntff'

print(f"Step 4: Found NEFF: {neff_path}")

# Print the command to run (for manual execution or subprocess)
capture_cmd = f"neuron-explorer capture -n {neff_path} -s {ntff_path} --profile-nth-exec=2 --enable-dge-notifs"
print(f"\nCapture command:\n  {capture_cmd}")


# =============================================================================
# Step 5: View results command
# =============================================================================

view_cmd = f"neuron-explorer view --output-format summary-json -n {neff_path} -s {ntff_path}"
print(f"\nView command (run after capture):\n  {view_cmd}")


# =============================================================================
# Summary
# =============================================================================

print("""
=============================================================================
PROFILING WORKFLOW SUMMARY
=============================================================================

1. Environment variables set (before imports):
   - NEURON_RT_INSPECT_ENABLE=1
   - NEURON_RT_INSPECT_DEVICE_PROFILE=1
   - NEURON_RT_INSPECT_OUTPUT_DIR=./output

2. Kernel executed, NEFF generated in ./output/<instance_pid>/

3. Profile directory created for this iteration

4. Next steps (run manually):
   a. Capture profile: neuron-explorer capture -n <neff> -s <ntff> --profile-nth-exec=2 --enable-dge-notifs
   b. View JSON results: neuron-explorer view --output-format summary-json -n <neff> -s <ntff>
=============================================================================
""")
