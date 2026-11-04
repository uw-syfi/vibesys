#!/usr/bin/env python3
"""Identify NKI kernel NEFFs in a Neuron runtime output directory.

Traces each NEFF to its compile workdir and reads the HLO module to determine
whether it's an NKI custom kernel or a standard XLA-compiled graph.

For NKI kernels, extracts: func_name, input_names, output_names, platform_target.
For XLA graphs, extracts: HLO op types (dot, maximum, etc.).

Usage:
  python3 identify-neffs.py <output_dir>                    # List all NEFFs
  python3 identify-neffs.py <output_dir> <kernel_name>      # Print path of specific kernel

Examples:
  python3 identify-neffs.py ./output
  python3 identify-neffs.py ./output matmul_relu

Note: Requires compile workdirs in /tmp/$USER/neuroncc_compile_workdir/ to still exist.
      Run this promptly after kernel execution, before temp cleanup.
"""
import subprocess, os, re, base64, json, glob, sys


def identify_neff(neff_path):
    """Identify a NEFF file by tracing to its compile workdir and reading the HLO module."""
    info = {
        "path": neff_path,
        "is_nki": False,
        "func_name": None,
        "input_names": [],
        "output_names": [],
        "xla_ops": [],
    }

    # Step 1: Extract compile workdir path embedded in NEFF binary
    result = subprocess.run(["strings", "-n", "8", neff_path], capture_output=True, text=True)
    match = re.search(r"(/tmp/\S+/neuroncc_compile_workdir/[^/]+)", result.stdout)
    if not match or not os.path.isdir(match.group(1)):
        return info

    workdir = match.group(1)

    # Step 2: Find HLO module protobuf in compile workdir
    hlo_files = glob.glob(os.path.join(workdir, "*.hlo_module.pb"))
    if not hlo_files:
        return info

    result = subprocess.run(["strings", hlo_files[0]], capture_output=True, text=True)
    hlo_strings = result.stdout

    # Step 3: Check for NKI marker (custom-call with AwsNeuronCustomNativeKernel)
    info["is_nki"] = "AwsNeuronCustomNativeKernel" in hlo_strings

    if info["is_nki"]:
        # Step 4: Decode base64 kernel metadata blob
        for line in hlo_strings.splitlines():
            if len(line) > 80 and re.match(r"^[A-Za-z0-9+/=]+$", line):
                try:
                    metadata = json.loads(base64.b64decode(line).decode())
                    info["func_name"] = metadata.get("func_name")
                    klir = metadata.get("klir_binary", {})
                    info["input_names"] = klir.get("input_names", [])
                    info["output_names"] = klir.get("output_names", [])
                    info["platform_target"] = metadata.get("platform_target")
                    break
                except Exception:
                    pass
    else:
        # Extract HLO op types for XLA graphs
        ops = set()
        for line in hlo_strings.splitlines():
            if re.match(
                r"^(dot|maximum|add|multiply|reduce|broadcast|transpose|convolution|reshape|slice)",
                line,
            ):
                ops.add(line.split(".")[0])
        info["xla_ops"] = sorted(ops)

    return info


def main():
    output_dir = sys.argv[1] if len(sys.argv) > 1 else "./output"
    target_name = sys.argv[2] if len(sys.argv) > 2 else None

    neff_files = sorted(glob.glob(os.path.join(output_dir, "**/*.neff"), recursive=True))

    if not neff_files:
        print(f"No NEFF files found in {output_dir}", file=sys.stderr)
        sys.exit(1)

    for neff_path in neff_files:
        info = identify_neff(neff_path)

        if target_name:
            # Mode: find specific kernel by name → print path and exit
            if info["func_name"] == target_name:
                print(neff_path)
                sys.exit(0)
        else:
            # Mode: list all NEFFs with identification
            if info["is_nki"]:
                tag = f"[NKI:{info['func_name']}]"
                detail = f"  inputs: {info['input_names']}  outputs: {info['output_names']}"
            else:
                tag = f"[XLA:{','.join(info['xla_ops'])}]" if info["xla_ops"] else "[XLA:unknown]"
                detail = ""

            # Find matching NTFF
            neff_basename = os.path.basename(neff_path)
            neff_id = neff_basename.replace("neff_", "").replace(".neff", "")
            ntff_path = os.path.join(os.path.dirname(neff_path), f"{neff_id}.ntff")
            ntff_info = f"  ntff: {ntff_path}" if os.path.exists(ntff_path) else ""

            print(f"{tag} {neff_path}")
            if detail:
                print(detail)
            if ntff_info:
                print(ntff_info)

    if target_name:
        print(f"No NEFF found for kernel '{target_name}'", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
