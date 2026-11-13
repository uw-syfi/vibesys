# FPGA HLS domain prompts

Use for tasks whose candidate is an FPGA accelerator written in high-level
synthesis C++ (Vitis HLS) against a task-owned reference, a target device part
and clock, and an evaluator that runs C simulation, synthesis, co-simulation
and implementation. Select with `[agent].domain = "fpga-hls"` in the input
bundle.

The role files provide HLS-specific correctness, feasibility and optimization
guidance. The task bundle owns the top function signature, target part, clock,
editable files, reference, checker and benchmark. The bundled HLS skills are
selected by domain through `skills/domains/.vibesys.toml`.
