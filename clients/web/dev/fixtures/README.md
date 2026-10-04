# Qwen3.5 campaign fixture

## Browser campaign replay

`trajectory-replay.json` is a normalized campaign record loaded by the demo
adapter. The dashboard and history controller consume the source-neutral
`CampaignRecord` contract, so they do not branch on fixture versus live data.

The fixture contains all 82 points from the performance plot in Claude session
`a2d3319a-c2c4-444f-a440-4881f158f32c`, followed by 29 paired and candidate
measurements from Round 15 in continuation session
`9ae9a100-f067-4aa1-8334-2589bd573a6c`. The original session ends at
1154.477 tok/s. The later continuation supplies the 2242.4 tok/s MTP result and
the 2000 tok/s campaign target.

The chart uses the later benchmark's scale for one continuous view. The v5 to
v6 boundary remains explicit because the scoring definition changed. SGLang is
not included.

The transcripts do not identify the agent that triggered or ran each plotted
measurement, so those attribution fields are null. Workstream taxonomy and
curated agent turns use transcript evidence, but they are not presented as
verbatim backend events. This record remains a frontend fixture and does not
change `src/vibesys/`, `src/server/`, or the generated wire protocol.
