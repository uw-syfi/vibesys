# Qwen3.5 dynamic trajectory fixture

## Browser campaign replay

`trajectory-replay.json` is the web UI's structured campaign replay. It has a
strict frontend-owned contract parsed by `parseReplayScenario` from
`clients/web/src/replay-scenario.ts`. The event log remains the source for
wire-level run state; this sidecar carries campaign-level objective metadata,
benchmark definitions and boundary, stable workstream lifecycles, measurement
attribution, and curated turn-by-turn agent trajectories.

The objective has no numeric target. The 1249.317 tok/s campaign line is a
reference, not a declared target. Selected Qwen3.5 throughput values and the
v4/v5 comparison caveat come from campaign notes. Workstream boundaries, gate
details, agent identities, and conversational turns are illustrative
reconstructions, not recovered backend events. The fixture states this
provenance explicitly so the UI can show rich interactions without presenting
synthetic conversation as recorded history.

The v4-to-v5 boundary is after sequence 12. Values on opposite sides of it must
not be compared as a single optimization delta.

`qwen35-dynamic-trajectory.jsonl` is a frontend development replay of the
Qwen3.5 397B MI300A optimization campaign discussed in Claude session
`a2d3319a-c2c4-444f-a440-4881f158f32c`.

It contains current `server.events.RunEvent` records only. The 82
`benchmark_result` values and their selected chronological order reproduce the
final performance plot. Source order 138 is placed between 119 and 120 because
the transcript says that MTP run occurred there. Landmark labels, candidate
dispositions, the SGLang v6 reference (961.013 tok/s), and the target
(1249.317 tok/s) come from the session and campaign notes.

The four campaign-long lifetimes and two nested short diagnostics are a
plausible projection of the dynamic orchestration workstreams, not recovered
backend events. All timestamps, execution IDs, prompts, and exact boundaries
are synthetic. The current event contract has no first-class workstream or
candidate-disposition field, so those facts are represented with existing
execution metadata and event `text`.

The apparent increase from about 790 to 1042 tok/s combines C64 to C96 scaling
with a v5 to v6 benchmark correction. Do not attribute it to one optimization.
The clean paired late gain is turn-suffix folding:
1053.390534263958 to 1154.4770391356592 tok/s, about 9.6%.

This fixture is intentionally not wired into the production replay endpoint.
It lets frontend work proceed without changing `src/vibesys/`,
`src/server/`, or the generated wire protocol.
