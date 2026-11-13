# Vendored: AMD ROSS Agentic AI Assistant (Vitis HLS skills)

These are vendored Agent Skills from AMD's open-source
[`Xilinx/ross-ai-assistant`](https://github.com/Xilinx/ross-ai-assistant),
licensed under **MIT** (see `LICENSE`).

They teach an agent Vitis HLS design rules: where pragmas may go, what C++
synthesizes, and when pipelining, dataflow, streaming, burst access and loop
flattening apply. VibeSys installs them only for runs whose input bundle sets
`[agent].domain = "fpga-hls"` (see `.vibesys.toml` and
`docs/contributing/skill-metadata.md`).

## What's vendored

| Skill | Purpose |
| ----- | ------- |
| `hls-pragma-scope` | Where each HLS pragma may be placed. |
| `hls-synthesizable` | Whether C/C++ code is synthesizable. |
| `hls-pipeline` | Applying the pipeline pragma: validity, II, rewind, style. |
| `hls-ii-diagnose` | Root cause of a missed initiation interval, from synthesis evidence. |
| `hls-dataflow` | Whether a region is a canonical dataflow region. |
| `hls-array-to-stream` | Coding style for converting arrays to streams. |
| `hls-burst-inference` | Whether AXI burst inference applies to a memory access. |
| `hls-flattenable` | Whether a loop nest can be flattened. |

The rest of upstream is intentionally omitted. The flow-running and
report-reading skills (`hls-run-flow`, `hls-*-report`,
`hls-component-basic-info`) and the loops built on them (`hls-optimize`,
`hls-architect`) assume the agent runs Vitis itself; in VibeSys the task
evaluator owns every flow. The stencil, line-buffer and MATLAB skills target
other workloads, and the Vivado RTL, Versal AI Engine, Vitis AI, embedded and
board-debug skills are outside the domain's current scope.

## Provenance

- Upstream: https://github.com/Xilinx/ross-ai-assistant
- Pinned commit: `dce0fb90611b144dd529490359b51cea5d0d5596` (2026-10-23)
- License: MIT

**Do not hand-edit** the files under `skills/`: they are a vendored copy.
Re-vendor with `./update.sh` to pull a newer upstream revision. The script
applies one fix-up: it adds a VibeSys note after each skill's frontmatter
saying that the evaluator runs the Vitis flows and that the companion skills
upstream names are not installed. Upstream text is otherwise unchanged.
