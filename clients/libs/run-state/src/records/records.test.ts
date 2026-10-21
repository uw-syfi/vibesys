import {describe, expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {RecordSchema} from '@vibesys/proto';
import {decodeRecordRows, recordRows} from './index.js';

const record = (seq: bigint) =>
  toBinary(
    RecordSchema,
    create(RecordSchema, {
      seq,
      epoch: 7,
      body: {case: 'legacyEvent', value: {json: '{"type":"run_started"}'}},
    }),
  );

describe('raw records', () => {
  test('projects exact 64-bit sequence numbers, epoch, body and legacy event type with bounded retention', () => {
    const records = Array.from({length: 1200}, (_, seq) => record((1n << 60n) + BigInt(seq)));
    const rows = recordRows([], decodeRecordRows(records));
    expect(rows).toHaveLength(1000);
    expect(rows[0]).toEqual({
      seq: String((1n << 60n) + 200n),
      epoch: 7,
      body: 'legacyEvent',
      event: 'run_started',
    });
    let incremental: ReturnType<typeof recordRows> = [];
    for (const bytes of records) incremental = recordRows(incremental, decodeRecordRows([bytes]));
    expect(incremental).toEqual(rows);
  });
});
