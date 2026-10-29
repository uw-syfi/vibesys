import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {create, fromBinary, fromJsonString} from '@bufbuild/protobuf';
import {KeyframeSchema, RecordSchema, RunHeadSchema} from '@vibesys/proto';
import {foldSkeleton, observeRun, seedSkeleton} from './index.js';

// Shared with vs-runlog's Python fold and the gateway's writer-file tests.
const fixtures = new URL('../../../../proto/testdata/skeleton/', import.meta.url);

for (const name of ['core', 'epochs', 'legacy']) {
  test(`Python writer keyframes and the TypeScript fold agree: ${name}`, () => {
    const expected = fromBinary(
      KeyframeSchema,
      readFileSync(new URL(`${name}/expected.pb`, fixtures)),
    );
    const records = (
      JSON.parse(readFileSync(new URL(`${name}/records.json`, fixtures), 'utf8')) as unknown[]
    ).map(value => fromJsonString(RecordSchema, JSON.stringify(value)));
    const full = foldSkeleton(seedSkeleton(), records);
    expect(
      create(KeyframeSchema, {
        ...full,
        throughSeq: full.throughSeq ?? 0n,
        logOffset: expected.logOffset,
      }),
    ).toEqual(expected);
    expect(full).toEqual(seedSkeleton(expected));
    if (name !== 'legacy') {
      const head = fromBinary(RunHeadSchema, readFileSync(new URL(`${name}/run.head`, fixtures)));
      expect(observeRun(full).epoch).toBe(head.epoch);
      expect(observeRun(full).lifecycle).toEqual(head.lifecycle);
    }

    // Every emitted checkpoint can bootstrap the same final skeleton.
    for (const record of records) {
      if (
        record.body.case !== 'lifecycle' &&
        record.body.case !== 'epochStarted' &&
        record.body.case !== 'declarationAmended'
      )
        continue;
      const keyframe = fromBinary(
        KeyframeSchema,
        readFileSync(new URL(`${name}/keyframes/${record.seq}.pb`, fixtures)),
      );
      expect(
        foldSkeleton(
          seedSkeleton(keyframe),
          records.filter(item => item.seq > record.seq),
        ),
      ).toEqual(full);
    }
  });
}
