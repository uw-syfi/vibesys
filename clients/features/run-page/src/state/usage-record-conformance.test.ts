import {expect, it} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent} from '@vibesys/contracts';
import {parseHelperMessage, type UsageRecord} from '@vibesys/contracts/host-helper';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';

/**
 * The usage records a journal projects to (`server.run_usage`) are one contract with two readers:
 * the shared fixture under `tests/conformance/fixtures/usage-records` holds a trimmed real journal
 * and the records the Python projection returns for it. This test reads those records through the
 * generated schema, so a field the Python model adds, drops or retypes fails here until the
 * generated types and the fixture agree.
 */
const FIXTURE = resolve(
  import.meta.dir,
  '../../../../../tests/conformance/fixtures/usage-records/codex-ten-rounds-trimmed.json',
);

interface Fixture {
  readonly events: RunEvent[];
  readonly records: UsageRecord[];
}

const fixture = JSON.parse(readFileSync(FIXTURE, 'utf8')) as Fixture;

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

it('reads the records of the shared fixture through the generated schema, unchanged', () => {
  const message = parseHelperMessage(summaryPage(fixture.records));

  if (message.type !== 'runs.summary') throw new Error(`parsed as ${message.type}`);
  expect(message.records).toEqual(fixture.records);
  expect(message.record_count).toBe(fixture.records.length);
});

it('names a record field the schema does not know', () => {
  const stray = {...fixture.records[0], unexpected: 1} as unknown as UsageRecord;

  expect(() => parseHelperMessage(summaryPage([stray]))).toThrow('unexpected');
});

it('has a record that kept its spend but not its execution start', () => {
  const orphan = fixture.records.find(record => record.role === null);

  expect(orphan?.tokens.input).not.toBe(null);
  expect([orphan?.harness, orphan?.model, orphan?.started_at]).toEqual([null, null, null]);
});

it("folds the fixture's events, as the client does, to the records the host projects from them", () => {
  const core = reduceEventBatch(initialCoreState(), fixture.events);

  expect(core.usageRecords).toEqual(fixture.records);
});
