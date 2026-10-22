import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
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
      message: '{"type":"run_started"}',
      failure: false,
    });
    let incremental: ReturnType<typeof recordRows> = [];
    for (const bytes of records) incremental = recordRows(incremental, decodeRecordRows([bytes]));
    expect(incremental).toEqual(rows);
  });
});

test('every retained record arm has a description, including startup and failures', () => {
  const fixtures = new URL('../../../../../proto/testdata/records/', import.meta.url);
  const bytes: Uint8Array[] = readdirSync(fixtures)
    .filter(name => name.endsWith('.pb'))
    .map(name => readFileSync(new URL(name, fixtures)));
  bytes.push(
    toBinary(
      RecordSchema,
      create(RecordSchema, {
        body: {case: 'epochStarted', value: {epoch: 3}},
      }),
    ),
  );
  const rows = decodeRecordRows(bytes);
  expect(new Set<string>(rows.map(row => row.body))).toEqual(
    new Set(
      RecordSchema.oneofs
        .find(oneof => oneof.localName === 'body')
        ?.fields.map(field => field.localName),
    ),
  );
  expect(rows.every(row => row.message.length > 0 && !row.message.includes('unknown record'))).toBe(
    true,
  );
  expect(rows.some(row => row.body === 'lifecycle' && row.message.includes('Starting'))).toBe(true);
  expect(rows.some(row => row.failure)).toBe(true);
  expect(() => decodeRecordRows([toBinary(RecordSchema, create(RecordSchema))])).toThrow(
    'body required',
  );
});
