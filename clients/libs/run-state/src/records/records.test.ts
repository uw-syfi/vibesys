import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {create, fromBinary, fromJsonString, toBinary} from '@bufbuild/protobuf';
import {RecordSchema, TailResponseSchema} from '@vibesys/proto';
import {
  decodeRecordRows,
  RECORD_BODY_KIND,
  type RecordBodyCase,
  type RecordRow,
  recordRows,
  rowsOfRecords,
  tailRecordRows,
} from './index.js';

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

const RECORD_FIXTURES = new URL('../../../../../proto/testdata/records/', import.meta.url);
const CORE_FIXTURES = new Set([
  'core_input.pb',
  'step_started.pb',
  'step_finished.pb',
  'attempt_interrupted.pb',
  'run_ended.pb',
]);

function fixtureBytes(core: boolean): Uint8Array[] {
  return readdirSync(RECORD_FIXTURES)
    .filter(name => name.endsWith('.pb') && CORE_FIXTURES.has(name) === core)
    .map(name => readFileSync(new URL(name, RECORD_FIXTURES)));
}

/** mulberry32: a seeded generator, so a failing interleaving repeats (fast-check is not a dependency). */
function seeded(seed: number): (bound: number) => number {
  let state = seed >>> 0;
  return bound => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) % bound;
  };
}

test('every retained record arm has a description, including startup and failures', () => {
  const bytes = fixtureBytes(false);
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
      Object.keys(RECORD_BODY_KIND).filter(
        body => RECORD_BODY_KIND[body as RecordBodyCase] === 'row',
      ),
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

describe('core records are runtime bookkeeping', () => {
  test('the classification covers every body of the store schema, and core is the five core kinds', () => {
    const bodies = RecordSchema.oneofs
      .find(oneof => oneof.localName === 'body')
      ?.fields.map(field => field.localName);
    expect(Object.keys(RECORD_BODY_KIND).sort()).toEqual([...(bodies ?? [])].sort());
    expect(
      Object.entries(RECORD_BODY_KIND)
        .filter(([, kind]) => kind === 'core')
        .map(([body]) => body)
        .sort(),
    ).toEqual(['attemptInterrupted', 'coreInput', 'runEnded', 'stepFinished', 'stepStarted']);
  });

  test('a log of core records alone has no rows, whatever the reader', () => {
    const core = fixtureBytes(true);
    expect(core).toHaveLength(CORE_FIXTURES.size);
    expect(decodeRecordRows(core)).toEqual([]);
    expect(
      tailRecordRows(toBinary(TailResponseSchema, create(TailResponseSchema, {records: core}))),
    ).toEqual([]);
    expect(rowsOfRecords(core.map(bytes => fromBinary(RecordSchema, bytes)))).toEqual([]);
  });

  test('property: N core records among M ordinary ones leave exactly the M ordinary rows, in order', () => {
    const ordinary = fixtureBytes(false);
    const core = fixtureBytes(true);
    const only = withoutSeq(decodeRecordRows(ordinary));
    for (let seed = 1; seed <= 200; seed += 1) {
      const next = seeded(seed);
      const log = interleaved(next, ordinary, core);
      expect(withoutSeq(decodeRecordRows(log))).toEqual(only);
      // Fed in batches of any size, the bounded view is the same.
      let rows: readonly RecordRow[] = [];
      for (let at = 0; at < log.length; ) {
        const size = 1 + next(5);
        rows = recordRows(rows, decodeRecordRows(log.slice(at, at + size)));
        at += size;
      }
      expect(withoutSeq(rows)).toEqual(only);
    }
  });
});

const withoutSeq = (rows: readonly RecordRow[]) => rows.map(row => ({...row, seq: ''}));

/** The ordinary records in order, core records among and after them, numbered 1, 2, 3, ... as a log is. */
function interleaved(
  next: (bound: number) => number,
  ordinary: readonly Uint8Array[],
  core: readonly Uint8Array[],
): Uint8Array[] {
  const log: Uint8Array[] = [];
  const pickCore = () => core[next(core.length)] ?? new Uint8Array();
  let taken = 0;
  while (taken < ordinary.length)
    log.push(next(3) === 0 ? pickCore() : (ordinary[taken++] ?? new Uint8Array()));
  for (let extra = next(4); extra > 0; extra -= 1) log.push(pickCore());
  return log.map((bytes, index) =>
    toBinary(
      RecordSchema,
      create(RecordSchema, {...fromBinary(RecordSchema, bytes), seq: BigInt(index + 1)}),
    ),
  );
}

test('retained skeleton formats render every record, including opaque legacy text', () => {
  const fixtures = new URL('../../../../../proto/testdata/skeleton/', import.meta.url);
  for (const name of ['core', 'epochs', 'legacy']) {
    const json: unknown[] = JSON.parse(
      readFileSync(new URL(`${name}/records.json`, fixtures), 'utf8'),
    );
    const bytes = json.map(value =>
      toBinary(RecordSchema, fromJsonString(RecordSchema, JSON.stringify(value))),
    );
    const rows = decodeRecordRows(bytes);
    expect(rows).toHaveLength(json.length);
    if (name === 'legacy') {
      expect(rows[0]?.message).toBe('legacy');
      expect(rows[0]?.event).toBe('');
    }
  }
});
