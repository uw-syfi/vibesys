# Roofline

Places a kernel or op against the compute and bandwidth ceilings so "bound" is
measured, not guessed. Feeds [`counter-triage.md`](counter-triage.md), which
turns the position into a verdict and a next step.

## Prerequisites

A capture from [`profiler.md`](profiler.md): either `compute.py`'s
`analyze` output (per-kernel, empirical, from an on-device microbenchmark) or
`torch_profiler`'s `roofline` subcommand (op-level, computed against this
file's tables from a torch trace's recorded shapes and dtypes). Confirm the
kernel that ran is the one you intend (see
[`aiter-engagement.md`](aiter-engagement.md)) before placing a point on
these roofs; a fallback kernel's roofline position says nothing about the
kernel you meant to measure.

## The model in three lines

- **Sloped bandwidth roof**: `achievable = AI × HBM bandwidth`.
- **Flat compute roof**: per-dtype peak FLOP/s.
- **Ridge point** = where they cross (`peak FLOP/s ÷ bandwidth`, in FLOP/byte).
  Left of it → bandwidth-bound. Right → compute-bound.

`AI` (arithmetic intensity) = FLOPs ÷ bytes moved from HBM.

## Per-arch peaks and ridges

Two generations matter here: gfx90a (MI210, this repo's measured test
machine) and gfx942 (MI300X/MI300A/MI325X). They are different CDNA
generations with different precision support: do not carry a gfx942 number
onto gfx90a or vice versa.

### gfx90a (MI210, CDNA2): 104 CUs, 64 GB HBM2e

| dtype | peak (spec) | peak (measured, sustained under serving load) | HBM BW (spec) | HBM BW (measured) | ridge, spec | ridge, measured |
|:--|:--|:--|:--|:--|:--|:--|
| BF16 / FP16 | 181 TFLOP/s | 115–122 TFLOP/s (power-limited; isolated bursts ~150) | 1600 GB/s | 1380 GB/s | ~113 FLOP/byte | ~83–88 FLOP/byte |

FP8 is **not native** on MI210 (`fp8_native = false`): there is no FP8 roof
to draw here; a GEMM ported from an FP8 gfx942 recipe runs in BF16/FP16 on
this SKU or not at all. Source:
`examples/model-serving/qwen3.5-9b-mi210/config/platforms/mi210.toml`.

**Use the measured ridge (~83–88 FLOP/byte) to classify real kernels on this
hardware, not the spec ridge (~113).** The spec peak assumes a clock this SKU
does not sustain under continuous load.

### gfx942 (MI300X / MI300A / MI325X, CDNA3): spec only, unmeasured in this repo

| SKU | dtype | peak (spec) | HBM BW (spec) | ridge (spec) |
|:--|:--|:--|:--|:--|
| MI300X | BF16/FP16 dense | ~1.3 PFLOP/s | 5.3 TB/s | ~245 FLOP/byte |
| MI300X | FP8 | ~2.6 PFLOP/s (roughly double BF16) | 5.3 TB/s | ~491 FLOP/byte |
| MI325X | BF16/FP16 dense | ~1.3 PFLOP/s (compute unchanged from MI300X; verify against your SKU's datasheet) | 6.0 TB/s | ~217 FLOP/byte |

These numbers come from [`hardware.md`](hardware.md), which is itself marked
experimental for this repo: no MI300-family run has produced a measured
sustained figure here yet. Treat the peaks as upper bounds and the ridges as
directional until you have an empirical `compute.py --roof-only`-style
capture on the actual SKU. MI325X's ridge moves left of MI300X's (more
bandwidth per FLOP): a kernel that reads compute-bound on MI300X can land
closer to bandwidth-bound on MI325X purely from the BW increase; re-classify
rather than porting the verdict.

## Practical ceiling: measured vs spec

MI210 gives this repo a real number instead of a folklore constant: sustained
GEMM throughput under continuous serving load reaches **~64–67% of the spec
peak** (115–122 of 181 TFLOP/s), power-limited rather than compute-limited;
isolated bursts reach **~83%** (150 of 181). **Use the sustained figure as
the practical ceiling** when judging whether a decode/serving kernel has
headroom on this SKU: the gap to spec peak here is a power/thermal cap, not
unclaimed software headroom.

No equivalent measured ceiling exists yet for gfx942 in this repo. Do not
import a ceiling fraction from another architecture generation (including
prior AMD generations' tuned-GEMM figures) onto MI300-family hardware:
capture one before treating any peak-relative claim as more than a rough
prior.

## Reading a point

| Where it sits | Verdict | Next |
|:--|:--|:--|
| On the sloped (bandwidth) roof | bandwidth-bound | raise AI: fuse ops, better cache reuse, check quantization; see [`counter-triage.md`](counter-triage.md) |
| On the flat (compute) roof | compute-bound | confirm the best available kernel library is engaged before concluding this is a hardware limit; see [`aiter-engagement.md`](aiter-engagement.md) |
| Under both roofs | occupancy- or latency-bound | counters disambiguate; see [`counter-triage.md`](counter-triage.md) |

Two things that trip people up:

- **A dtype change moves the point diagonally and shifts the ridge.**
  BF16→FP8 (gfx942 only, not available on gfx90a) halves bytes and roughly
  doubles peak; the point can cross into a different class.
- **Compute achieved TFLOP/s from measured wall time, never from the spec
  clock**: see [`measurement-protocol.md`](measurement-protocol.md).

## Building it empirically

`compute.py`'s `analyze` step surfaces rocprof-compute's own empirical
roofline block (measured on-device peaks, not datasheet numbers) when the
`profile` pass included the roofline microbenchmark: use this for a specific
kernel. `torch_profiler`'s `roofline` subcommand plots an op's achieved
throughput from a torch trace against this file's tables: use this when a
full rocprof-compute pass isn't warranted, or when the op of interest doesn't
isolate cleanly as a single dispatch. Prefer the empirical (device-measured)
roofline when both are available; the table-based one is only as accurate as
the peaks in this file.

## Verify

| Check | Pass |
|:--|:--|
| Empirical compute roof vs the spec peak in this file | at or below spec, a sane fraction (above means distrust the run) |
| Achievable HBM BW vs spec | below spec BW (at or above means distrust the run) |
| Kernel marker position | matches where its measured AI predicts |
| Point after a fix | moved up or right toward a roof, not merely a lower wall time |

## Failure modes

| Symptom | Cause | Fix |
|:--|:--|:--|
| "40% of peak, must be broken" | used spec peak as the bar on MI210 | compare against the measured sustained ceiling (~64–67%) instead |
| Kernel looks hopeless on an FP8 roof | ported a gfx942 recipe to gfx90a, which has no native FP8 | draw the BF16/FP16 roof for MI210 |
| BW-bound verdict, HBM counter low | working set is cache-resident, not HBM-bound | check cache hit rate before trusting the HBM line |
| Point didn't move after a fix | wrong bottleneck, or the change wasn't live | [`counter-triage.md`](counter-triage.md); confirm via [`aiter-engagement.md`](aiter-engagement.md) |
| Roofs differ run to run | cold clocks / unlocked DVFS | [`measurement-protocol.md`](measurement-protocol.md) |

## See also

- [`counter-triage.md`](counter-triage.md): acting on where a point lands
- [`measurement-protocol.md`](measurement-protocol.md): how to get a trustworthy point
- [`hardware.md`](hardware.md): the SKU matrix and precision support this table draws from
- [`aiter-engagement.md`](aiter-engagement.md): confirm the kernel plotted is the one you intended
- [`profiler.md`](profiler.md): `compute.py` and `torch_profiler`'s `roofline` subcommand
