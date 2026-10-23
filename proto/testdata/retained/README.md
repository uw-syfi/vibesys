# Retained writer corpus

These are immutable outputs of previous producers and `FileRunLogWriter` implementations.
They are reopened by `test_saved_previous_writers_reopen_with_the_same_facts`,
`TestSavedPreviousWritersRetainInputFacts`, and the client's `retained-writers.test.ts`.
The Python test is part of the ordinary runlog suite; the gateway and client tests run in their
ordinary suites. Every directory named in `manifest.json` is a compatibility case.

For a later store change, capture a measured run with the preceding producer and writer before
changing them. Add its log, head and keyframes in a directory named for the writer commit, and
add its input, candidate ratio, chart reference and first measurement sequence to the manifest.
Keep cases covering the seven-day read window; do not regenerate previous-writer bytes with the
current writer. The gateway test pins `skeleton.response.pb` with
`VIBESYS_UPDATE_GOLDEN=1 go test ./internal/server -run TestSavedPreviousWriters` from `gateway/`.
The client then reopens that exact public RPC reply and checks its live and historical screens.

`64c1a8b7` was written at `64c1a8b7fa850be42bab901b1723ed562dab6fa7` from that revision's
`storeRunFixture('facts')` records, with comparison/adoption spans omitted and sequences made
dense. It retains the actual INPUT-gate metric. Its published head has no `input_value`, which
exposes the loss introduced by removing client inference without upgrading old store data.
