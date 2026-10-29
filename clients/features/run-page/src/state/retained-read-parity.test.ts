import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {RunEvent} from '@vibesys/contracts';
import {KeyframeSchema, SkeletonResponseSchema} from '@vibesys/proto';
import {ingestSkeleton, seekSkeleton} from '@vibesys/run-skeleton';
import {hasRunEnded, initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {decodeRecordRows} from '@vibesys/run-state/records';
import {readSkeleton} from '@vibesys/run-state/skeleton';

const ROOT = new URL('../../../../../proto/testdata/conversion/', import.meta.url);

test('the same retained journal has the same legacy and native lifecycle and diagnostic logs', () => {
  const events: RunEvent[] = JSON.parse(
    gunzipSync(readFileSync(new URL('legacy.events.json.gz', ROOT))).toString(),
  );
  const values: string[] = JSON.parse(
    gunzipSync(readFileSync(new URL('records.base64.json.gz', ROOT))).toString(),
  );
  const records = values.map(value => Uint8Array.from(Buffer.from(value, 'base64')));
  const legacy = reduceEventBatch(initialCoreState(), events);
  const frame = fromBinary(KeyframeSchema, readFileSync(new URL('expected.pb', ROOT)));
  expect(legacy.status).toBe('completed');
  expect(hasRunEnded(legacy)).toBe(true);
  for (const checkpoint of [false, true]) {
    const load = ingestSkeleton(
      create(SkeletonResponseSchema, {
        ...(checkpoint ? {keyframe: toBinary(KeyframeSchema, frame)} : {records}),
        committedSeq: frame.throughSeq,
        nextSeq: frame.throughSeq + 1n,
        complete: true,
      }),
    );
    const reading = readSkeleton(load.skeleton, seekSkeleton(load.skeleton, frame.throughSeq));
    expect(String(reading.phase)).toBe(legacy.status);
  }
  const warnings = events
    .filter(event => event.type === 'framework_warning')
    .map(event => event.text ?? '');
  expect(warnings).toHaveLength(40);
  expect(
    decodeRecordRows(records)
      .filter(row => row.body === 'notice')
      .map(row => row.message),
  ).toEqual(warnings);
});
