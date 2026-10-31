import {describe, expect, it} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent} from '@vibesys/contracts';
import {parseHelperMessage, type UsageRecord} from '@vibesys/contracts/host-helper';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';

/**
 * The usage records a journal projects to (`server.run_usage`) are one contract with two readers:
 * the shared fixtures under `tests/conformance/fixtures/usage-records` hold journals and the
 * records the Python projection returns for them. This test reads those records through the
 * generated schema, so a field the Python model adds, drops or retypes fails here until the
 * generated types and the fixtures agree. One fixture is a real journal recorded before the
 * per-model split existed, so its lines have none of the split's fields; another holds lines that
 * have them. A reader must take both.
 */
const FIXTURES = resolve(
  import.meta.dir,
  '../../../../../tests/conformance/fixtures/usage-records',
);

interface Fixture {
  readonly events: RunEvent[];
  readonly records: UsageRecord[];
}

function fixtureNamed(name: string): Fixture {
  return JSON.parse(readFileSync(resolve(FIXTURES, name), 'utf8')) as Fixture;
}

const NAMES = readdirSync(FIXTURES)
  .filter(name => name.endsWith('.json'))
  .sort();
const OLD = fixtureNamed('codex-ten-rounds-trimmed.json');
const SPLIT = fixtureNamed('claude-by-model-synthetic.json');

function summaryPage(records: UsageRecord[]): string {
  const none = {input: null, cache_read: null, cache_write: null, output: null, reasoning: null};
  return JSON.stringify({
    type: 'runs.summary',
    request_id: 'u',
    through_sequence: 0,
    event_count: 0,
    journal_bytes: 0,
    trace: 'recorded',
    rounds_finished: 0,
    span_count: 0,
    reply_count: 0,
    usage_count: 0,
    record_count: records.length,
    tokens: none,
    overview: null,
    input_gates: [],
    terminal: null,
    spans: [],
    replies: [],
    schemas: {},
    usage: [],
    records,
    next_offset: null,
  });
}

describe.each(NAMES)('%s', name => {
  const fixture = fixtureNamed(name);

  it('reads the records of the shared fixture through the generated schema, unchanged', () => {
    const message = parseHelperMessage(summaryPage(fixture.records));

    if (message.type !== 'runs.summary') throw new Error(`parsed as ${message.type}`);
    expect(message.records).toEqual(fixture.records);
    expect(message.record_count).toBe(fixture.records.length);
  });

  it("folds the fixture's events, as the client does, to the records the host projects from them", () => {
    const core = reduceEventBatch(initialCoreState(), fixture.events);

    expect(core.usageRecords).toEqual(fixture.records);
  });
});

it('names a record field the schema does not know', () => {
  const stray = {...OLD.records[0], unexpected: 1} as unknown as UsageRecord;

  expect(() => parseHelperMessage(summaryPage([stray]))).toThrow('unexpected');
});

it('names a split row field the schema does not know', () => {
  const [first] = SPLIT.records;
  const [row] = first?.by_model ?? [];
  const stray = {...first, by_model: [{...row, unexpected: 1}]} as unknown as UsageRecord;

  expect(() => parseHelperMessage(summaryPage([stray]))).toThrow('unexpected');
});

it('has a record that kept its spend but not its execution start', () => {
  const orphan = OLD.records.find(record => record.role === null);

  expect(orphan?.tokens.input).not.toBe(null);
  expect([orphan?.harness, orphan?.model, orphan?.started_at]).toEqual([null, null, null]);
});

it('reads a record a host wrote before the split existed, which has none of its fields', () => {
  const older = OLD.records.map(record => {
    const {cost_basis: _basis, by_model: _rows, provider_session_id: _session, ...rest} = record;
    return rest as UsageRecord;
  });

  const message = parseHelperMessage(summaryPage(older));

  if (message.type !== 'runs.summary') throw new Error(`parsed as ${message.type}`);
  expect(message.records).toEqual(older);
});

it('keeps every model of a split turn, one primary, and counts its requests once', () => {
  for (const record of SPLIT.records) {
    const rows = record.by_model ?? [];
    if (rows.length === 0) continue;
    expect(rows.filter(row => row.primary)).toHaveLength(1);
    expect(rows.filter(row => !row.primary).every(row => row.turns === 0)).toBe(true);
  }
});

it('reads a malformed row as no split at all, never as part of one', () => {
  const event = SPLIT.events.find(each => each.data?.kind === 'turn_usage_recorded');
  const data = event?.data;
  if (event === undefined || data?.kind !== 'turn_usage_recorded') {
    throw new Error('the fixture has no usage event');
  }
  const [row] = data.by_model ?? [];
  if (row === undefined) throw new Error('the fixture usage event has no split');
  const broken: RunEvent = {...event, data: {...data, by_model: [row, {...row, model: ''}]}};

  const core = reduceEventBatch(initialCoreState(), [broken]);

  expect(core.usageRecords[0]?.by_model).toEqual([]);
  expect(core.usageRecords[0]?.cost_basis).toBe('price_table');
});
