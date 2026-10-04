# Version-2 fixture

`envelope-v2-main.json` was written through `vs_core.api.OperationRegistry.encode_envelope` using an untouched archive of main commit `cf6b304d3762df1c56ab2a3c4fbb1bb03176b0be`, before editing the contracts. It contains a charged active attempt, an accepted child observation and a waiting continuation. Its format is the actual version-2 encoder output, not a version-3 state with a changed header.

`envelope-v2-registered-turn.json` was also written by that untouched main encoder. It contains a registered session turn, its canonical accepted decision and dispatched intent, and two reserved input occurrences sharing one artifact. The registered request includes nested owner metadata named `evidence_id` and `observation_sequence`; migration preserves that opaque request payload unchanged and reconstructs only the exact occurrence transport manifest.
