---
name: fpga-hls-feasibility
description: Decide whether an HLS design fits the target FPGA and meets timing, and state an evidence-backed infeasibility verdict that names the binding limit. Use when resource use or timing slack is near a limit, when a performance target looks out of reach, or before reporting that a target cannot be met.
---

# FPGA HLS feasibility

A design is feasible on the target only if every resource class fits the
device and timing closes at the target clock. Speed does not compensate for
either.

## The limits

- **Resources:** LUT, FF, DSP, BRAM and URAM, each as a fraction of the target
  device's capacity. Over 100% of any class is infeasible. High utilization
  (roughly above 80% of LUTs) often fails routing or timing even when it fits.
- **Timing:** worst negative slack at the target clock. Negative slack after
  implementation means the design misses the clock. Synthesis estimates of
  the clock are not proof either way.
- **Memory ports:** a BRAM bank serves at most two accesses per cycle, so the
  number of banks an array is split into bounds the parallel reads per cycle,
  and so the achievable II.
- **Off-chip bandwidth:** bytes the computation must move divided by the
  memory interface's peak bandwidth is a lower bound on time, however much
  compute the design has.
- **Arithmetic:** multiply-accumulates per cycle are bounded by the DSPs
  available (times how many narrow multiplies one DSP packs) plus any built
  from LUTs.

## Stating a verdict

An infeasibility claim names the binding limit and shows the arithmetic, for
example: required DSPs per cycle for the target throughput, against the
device's DSP count; or required bytes per second, against the interface's
peak. Use the device's peak capacity, not a measured fraction of it, so the
bound holds for any better design.

Separate three outcomes:

- **Infeasible:** a bound like the ones above rules the target out for every
  design on this device and clock, under assumptions you state.
- **Not achieved:** no design found yet meets the target, but no bound rules
  it out. Keep searching or report the gap; do not call it infeasible.
- **Feasible:** a design meets the target, with the reports that show it.

State the assumptions a bound depends on, such as a fixed weight format or
batch size. Breaking an assumption may be a legitimate next direction if the
task contract allows it.
