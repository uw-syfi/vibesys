import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent} from '@vibesys/contracts';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {totalUsage, USAGE_DIMENSIONS} from '@vibesys/run-state/usage';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {runPageInitial, runPageStep} from './run-page.js';
import {summaryFacts, type WireSummary} from './run-summary.js';
import {tokensReading} from './token-words.js';
import {
  groupedBy,
  USAGE_CONTROLS_DEFAULT,
  USAGE_GROUPS,
  USAGE_METRICS,
  type UsageControls,
  usageView,
} from './usage-view.js';
import {headerView, usageRecords} from './views.js';

const FIXTURE = resolve(
  import.meta.dir,
  '../../../../../tests/conformance/fixtures/usage-records/codex-ten-rounds-trimmed.json',
);
const fixture = JSON.parse(readFileSync(FIXTURE, 'utf8')) as {
  events: RunEvent[];
  records: UsageRecord[];
};

function randomRecords(random: Seeded): UsageRecord[] {
  const count = random.int(30);
  return Array.from({length: count}, () => {
    const ended = random.bool()
      ? null
      : new Date(Date.UTC(2026, 9, 1, random.int(48))).toISOString();
    const some = (): number | null => (random.int(4) === 0 ? null : random.int(500_000));
    return {
      run_id: random.pick(['run-a', 'run-b']),
      execution_id: null,
      span_id: null,
      round_label: random.pick(['r1', 'r2', null]),
      role: random.pick(['planner', 'implementer', null]),
      harness: random.pick(['codex', 'claude', null]),
      model: random.pick(['model-a', 'model-b', null]),
      reasoning_effort: null,
      harness_version: null,
      started_at: null,
      ended_at: ended,
      tokens: {
        input: some(),
        cache_read: some(),
        cache_write: null,
        output: some(),
        reasoning: null,
      },
      cost_usd: random.bool() ? null : random.int(900) / 100,
      duration_ms: null,
    };
  });
}

function controlsOf(random: Seeded): UsageControls {
  const by = random.pick(USAGE_GROUPS).value;
  const within = random.pick([null, ...USAGE_GROUPS.map(group => group.value)]);
  return groupedBy({metric: random.pick(USAGE_METRICS).value, by, within}, by);
}

describe('the usage view', () => {
  test('every record is in one row and the totals are the records’, whatever the controls', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const records = randomRecords(random);
      const controls = controlsOf(random);
      const view = usageView(records, controls);
      expect(view.rows.reduce((sum, row) => sum + Number(row.turns), 0)).toBe(records.length);
      expect(view.turns).toBe(String(records.length));
      expect(view.spent).toEqual(tokensReading(totalUsage(records).tokens));
      expect(view.empty).toBe(records.length === 0);
    }
  });

  test('the bars and the records with no end time hold every record the rows do', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const records = randomRecords(random);
      const view = usageView(records, {...USAGE_CONTROLS_DEFAULT, metric: 'input'});
      const dated = records.filter(record => record.ended_at !== null).length;
      expect(view.undated).toBe(records.length - dated);
      expect(view.bars.length).toBeLessThanOrEqual(24);
      expect(view.span === null).toBe(dated === 0);
    }
  });

  test('a second grouping never repeats the first and still totals the same', () => {
    const records = randomRecords(new Seeded(11));
    for (const first of USAGE_GROUPS) {
      const controls = groupedBy({...USAGE_CONTROLS_DEFAULT, within: first.value}, first.value);
      expect(controls.within).toBeNull();
      const view = usageView(records, {...controls, within: 'model'});
      expect(view.turns).toBe(String(records.length));
    }
    expect(USAGE_DIMENSIONS).toContain('round');
  });
});

describe('the run’s records and the run’s spend', () => {
  const events = fixture.events;
  const total = totalUsage(fixture.records);
  const wire: WireSummary = {
    throughSequence: 1,
    eventCount: events.length,
    journalBytes: 1,
    trace: 'recorded',
    roundsFinished: 0,
    overview: null,
    spans: [],
    replies: [],
    tokens: total.tokens,
    usage: [],
    records: fixture.records,
  };
  const ended = {
    type: 'record',
    record: {
      kind: 'ended',
      run: {
        id: '0123456789ab',
        phase: 'stopped',
        end: {outcome: 'stopped', reason: 'Stopped by you.', resumable: false},
      },
    },
  } as never;

  function opened() {
    const initial = runPageInitial({
      identity: {host: 'ssh:host-b', runId: 'run-1'},
      title: {project: 'queue-rs', task: null},
      hostLabel: 'host-b',
      unavailable: {},
    });
    const asked = runPageStep(initial, ended);
    const request = asked.requests.find(each => each.kind === 'read-summary');
    if (request?.kind !== 'read-summary') throw new Error('no summary asked');
    return runPageStep(asked.state, {
      type: 'summary',
      token: request.token,
      reply: {kind: 'summary', ...summaryFacts(wire, 'run-1', 'stopped')},
      wallMs: 0,
    }).state;
  }

  test('an ended run opened from its summary: the face totals say what the header says', () => {
    const state = opened();
    const view = usageView(usageRecords(state), USAGE_CONTROLS_DEFAULT);

    expect(headerView(state).spent).toEqual(view.spent);
    expect(view.spent).not.toBeNull();
    expect(view.turns).toBe(String(fixture.records.length));
  });

  test('a run folded from its events: the face totals say what the header says', () => {
    const state = opened();
    const core = reduceEventBatch(initialCoreState(), events);
    const folded = {...state, summary: {kind: 'idle'}, run: {...state.run, core}} as typeof state;
    const view = usageView(usageRecords(folded), USAGE_CONTROLS_DEFAULT);

    expect(headerView(folded).spent).toEqual(view.spent);
    expect(usageRecords(folded)).toEqual(usageRecords(state));
  });
});
