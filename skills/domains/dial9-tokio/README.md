# dial9 (Tokio runtime trace skills + ranker)

This bundle has two parts: **vendored** dial9 Agent Skills under `skills/`, and a
**first-party** trace ranker under `ranker/` (see "First-party: the ranker").

The skills are **vendored** from the open-source
[`dial9-rs/dial9`](https://github.com/dial9-rs/dial9) project, licensed under
**Apache-2.0** (see `LICENSE.txt` / `NOTICE`).

They teach an agent to read **dial9** traces of a **Tokio** async runtime:
parse the binary trace format, run the analysis pipeline, scan for runtime red
flags (long polls, task leaks, scheduling delay, queue buildup, worker
imbalance), zoom into a single time window, and reason about the result against
how the runtime actually schedules work. The target is async Rust service
performance, where the interesting question is usually "which poll blocked the
worker, and why", not aggregate CPU time.

These skills are routed to the `database` domain (see `../.vibesys.toml` and
[Skill Metadata](../../../docs/contributing/skill-metadata.md)), so an agent
optimizing a database or streaming engine gets them without per-example wiring.

## What's vendored

Only the eight runtime-tracing skills from upstream `dial9-viewer/skills/`. The
`dial9-s3-analysis` skill (S3-hosted trace retrieval), the viewer web UI, and
the Rust crates are intentionally omitted.

| Skill | Purpose |
| ----- | ------- |
| `dial9-toolkit` | The JS modules (`analyze.js`, `trace_parser.js`, `trace_analysis.js`, `decode.js`); entry point for any trace. |
| `dial9-trace-loading` | Parse local trace files/buffers; the `ParsedTrace` data model. |
| `dial9-trace-analysis` | Analysis pipeline API: worker spans, CPU samples, scheduling delays, flamegraphs. |
| `dial9-trace-recipes` | Recipes for specific diagnostic questions over parsed traces. |
| `dial9-red-flags` | Automated health scan for common Tokio runtime problems. |
| `dial9-runtime` | Tokio execution model reference: waking, parking, cooperative scheduling, tail latency. |
| `dial9-diagnose-long-poll` | Root-cause a flagged long poll, including the off-CPU case. |
| `dial9-zoom-window` | Expand one timestamp into a per-worker, per-thread view; viewer deep links. |

`update.sh` applies two mechanical changes, and nothing else:

1. Upstream reaches the shared toolkit libraries and the viewer URL contract
   through symlinks into `dial9-viewer/ui/`, which is not vendored here, so the
   copy uses `cp -rL` and those arrive as regular files. A link-preserving copy
   would leave dangling symlinks and a toolkit that fails at `require` time.
2. `dial9-zoom-window`'s frontmatter `description` is a plain YAML scalar
   containing `": "`, which agent harnesses tolerate but the strict parser
   VibeSys validates frontmatter with rejects. The script double-quotes the
   scalar, preserving the text.

## First-party: the ranker

`ranker/` is **not** vendored. It is first-party VibeSys tooling that turns a
dial9 trace into the profile-guided result protocol, and it is what makes this
bundle useful for ranking rather than only for guided exploration. Upstream's
`analyze.js` reports per-span **wall-clock** time keyed on spawn location, which
double-counts nested spans and cannot say which operator held the CPU; the
ranker computes **on-CPU self time** geometrically (each nanosecond claimed by
exactly one span, grouped per task) and emits one stable, validated JSON report.

| Path | What it is |
| ---- | ---------- |
| `ranker/analyze_dial9.py` | Policy: which setup diagnostics invalidate a ranking, the cost model (in-window on-CPU poll seconds), the per-component evidence lines. Writes protocol v1. Stdlib only. |
| `ranker/extract/dial9_extract.js` | Toolkit-side extractor: parses segments, claims on-CPU nanoseconds per span, writes one stable report. Resolves the sibling vendored `dial9-toolkit` by default, so no `--toolkit`/`DIAL9_TOOLKIT` is needed when run in place. |
| `ranker/extract/check_invariants.js` | Independent cross-check of the extractor's claiming arithmetic. |

All dial9 and Node knowledge sits on one side of a JSON boundary (`extract/`),
all ranking policy on the other (`analyze_dial9.py`). `update.sh` wipes and
recreates only `skills/`, so `ranker/` survives a re-vendor untouched. Because
it lives under `skills/domains/`, which the repo's ruff and ty configs exempt
as vendored, the ranker is not linted or type-checked by repo tooling; it is
held to the repo baseline by hand.

## Provenance

- Upstream: https://github.com/dial9-rs/dial9
- Pinned commit: `ab680e74f9a1d05216512a83f42ad40701c07232`
- License: Apache-2.0

**Do not hand-edit the vendored files** under `skills/` — they are a vendored
copy; re-vendor with `./update.sh` to pull a newer upstream revision. The
first-party `ranker/`, `README.md`, and `NOTICE` are ours to edit.
