# Retained writer corpus

These are immutable outputs of previous producers and `FileRunLogWriter` implementations.
Each directory named in `manifest.json` is a compatibility case. Every consumer enumerates the
manifest, so a new case needs no test change:

- Python: `libs/vs-runlog/tests/test_retained_writers.py`, `test_retained_heads.py` and
  `test_observe.py`.
- Go: `TestSavedPreviousWritersRetainInputFacts` and the observation tests in
  `gateway/internal/server`, and `FuzzInputUpgradePreservesFactsAndBodies` in
  `gateway/internal/runlog`.
- Client: `retained-writers.test.ts`, which reopens the pinned `skeleton.response.pb`.

## A case

A directory holds the files the previous writer left: `run.log`, `run.head`, `keyframes/`, and
`skeleton.response.pb`, the gateway's public Skeleton reply over them. The reply is pinned with
`VIBESYS_UPDATE_GOLDEN=1 go test ./internal/server -run TestSavedPreviousWriters` from `gateway/`.
The client reopens that exact reply and checks its live and historical screens.

## Manifest fields

Every case has all fields. The directory name is the first eight characters of `writer_commit`.

| Field | Meaning |
| --- | --- |
| `writer_commit` | The revision whose `FileRunLogWriter` and `project_head` wrote the files. |
| `producer` | What made the records the writer was given, and the transform applied to them. |
| `input` | The input's measured value of the objective series. |
| `candidate` | The span id of the candidate whose ratio to the input the screens show. |
| `vs_baseline` | The candidate's ratio to the input, as shown beside the candidate. |
| `chart_reference` | The value the chart draws as the input's reference line. |
| `measurement_seq` | The sequence of the record that measures the input. The fact is absent before it. |
| `input_span` | The span id the measurement point is bound to, the input gate. |
| `head_input_absent` | True when the stored head has no `input_value` and does not state its input (`input_stated` false), as a head written before the fact existed. |

## Adding a case

Capture it before changing the producer or the writer, from a checkout of the preceding
revision. Never regenerate previous-writer bytes with the current writer. Keep cases covering the
seven-day read window.

1. Export the producer's records, one base64 `Record` per line. For the first case the producer
   is `storeRunFixture('facts')` of `@vibesys/run-fixtures`, run in a checkout of its
   `writer_commit` with the pinned bun from `packaging/fetch_bun.py`:

   ```ts
   // clients/libs/run-fixtures/src/export.test.ts, run with
   //   bun test --conditions=@vibesys/source src/export.test.ts
   import {toBinary} from '@bufbuild/protobuf';
   import {RecordSchema} from '@vibesys/proto';
   import {storeRunFixture} from './store-records.js';

   test('export', async () => {
     const lines = storeRunFixture('facts').records.map(record =>
       Buffer.from(toBinary(RecordSchema, record)).toString('base64'),
     );
     await Bun.write('records.b64', lines.join('\n') + '\n');
   });
   ```

2. In the same checkout, run `produce.py`. It applies the transform (drop the comparison and
   adoption spans, make sequences dense) and writes the case through that revision's writer:

   ```sh
   uv run python proto/testdata/retained/produce.py records.b64 OUT_DIR
   ```

   Copy `run.log`, `run.head` and `keyframes/` into the case directory. Do not copy the lock
   files. A rebuild of `64c1a8b7` is byte-identical to the committed `run.log` and keyframes; its
   `run.head` differs only in `process.pid`, the producing process's.
3. Add the manifest entry, then pin the Skeleton reply as above.

`64c1a8b7` retains the actual INPUT-gate metric. Its published head has no `input_value`, which
exposes the loss that removing client inference caused without upgrading old store data.
