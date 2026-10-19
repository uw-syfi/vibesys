import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent} from '@vibesys/contracts';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {foldUsage, totalUsage} from '@vibesys/run-state/usage';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {runPageInitial, runPageStep} from './run-page.js';
import {summaryFacts, type WireSummary} from './run-summary.js';
import {tokensReading} from './token-words.js';
import {
  groupedBy,
  TOKEN_KINDS,
  type TokenKind,
  USAGE_CHARTS,
  USAGE_CONTROLS_DEFAULT,
  USAGE_GROUPS,
  USAGE_METRICS,
  type UsageContext,
  type UsageControls,
  usageView,
  withKind,
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

function ideaOfRecord(ctx: UsageContext) {
  return (record: UsageRecord): string | null =>
    record.span_id === null ? null : (ctx.ideaOfSpan.get(record.span_id) ?? null);
}

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

const NO_CONTEXT: UsageContext = {ideaOfSpan: new Map(), ideas: new Map()};

/** A context in which every span with a number belongs to one of three ideas. */
function ideaContext(): UsageContext {
  return {
    ideaOfSpan: new Map(
      ['s0', 's1', 's2', 's3', 's4', 's5'].map((span, at) => [span, `idea-${at % 3}`]),
    ),
    ideas: new Map(),
  };
}

function controlsOf(random: Seeded): UsageControls {
  const by = random.pick(USAGE_GROUPS).value;
  const within = random.pick([null, ...USAGE_GROUPS.map(group => group.value)]);
  return groupedBy(
    {
      ...USAGE_CONTROLS_DEFAULT,
      metric: random.pick(USAGE_METRICS).value,
      kinds: TOKEN_KINDS.map(kind => kind.value)
        .filter(() => random.bool())
        .concat('input'),
      chart: random.pick(USAGE_CHARTS).value,
      within,
    },
    by,
  );
}

describe('the usage view', () => {
  test('every record is in one group and the total row is the records’, whatever the controls', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const records = randomRecords(random);
      const controls = controlsOf(random);
      const view = usageView(records, controls, ideaContext());
      const groups = view.rows.filter(row => row.kind === 'group');
      expect(groups.reduce((sum, row) => sum + Number(row.turns), 0)).toBe(records.length);
      expect(view.rows.at(-1)?.turns).toBe(String(records.length));
      expect(view.empty).toBe(records.length === 0);
    }
  });

  test('idea groups, with the turns before any idea, sum to the run total', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const records = randomRecords(random).map(record => ({
        ...record,
        span_id: random.pick(['s0', 's1', 's2', 's3', 's4', 's5', 's9', null]),
      }));
      const view = usageView(records, {...USAGE_CONTROLS_DEFAULT, by: 'idea'}, ideaContext());
      const groups = view.rows.filter(row => row.kind === 'group');
      const total = totalUsage(records);

      expect(groups.reduce((sum, row) => sum + Number(row.turns), 0)).toBe(total.turns);
      expect(foldUsage(records, ['idea'], ideaOfRecord(ideaContext())).total).toEqual(total);
      const setup = records.filter(
        record => !(record.span_id !== null && ideaContext().ideaOfSpan.has(record.span_id)),
      );
      expect(groups.some(row => row.label[0]?.text === 'Run setup')).toBe(setup.length > 0);
    }
  });

  test('the four kinds of tokens add up to the run’s in plus out', () => {
    for (const seed of SEEDS) {
      const records = randomRecords(new Seeded(seed));
      const total = totalUsage(records).tokens;
      const view = usageView(records, USAGE_CONTROLS_DEFAULT, NO_CONTEXT);
      expect(view.tiles[1]?.title).toBe(tokensReading(total)?.title ?? '');
    }
  });

  test('the chart holds every dated record: its slices and the undated turns', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const records = randomRecords(random);
      const view = usageView(records, {...USAGE_CONTROLS_DEFAULT, metric: 'tokens'}, NO_CONTEXT);
      const dated = records.filter(record => record.ended_at !== null).length;
      expect(view.undated).toBe(records.length - dated);
      expect(view.slices.length).toBeLessThanOrEqual(24);
      expect(view.span === null).toBe(dated === 0);
    }
  });

  test('a running total never falls and ends at what the slices add up to', () => {
    for (const seed of SEEDS) {
      const records = randomRecords(new Seeded(seed));
      const per = usageView(records, {...USAGE_CONTROLS_DEFAULT, metric: 'tokens'}, NO_CONTEXT);
      const run = usageView(
        records,
        {...USAGE_CONTROLS_DEFAULT, metric: 'tokens', chart: 'running'},
        NO_CONTEXT,
      );
      const spent = per.slices.reduce((sum, slice) => sum + slice.total, 0);
      expect(run.slices.at(-1)?.total ?? 0).toBe(spent);
      run.slices.forEach((slice, at) => {
        expect(slice.total).toBeGreaterThanOrEqual(run.slices[at - 1]?.total ?? 0);
      });
    }
  });

  test('a second grouping never repeats the first, and the last token kind stays on', () => {
    for (const first of USAGE_GROUPS) {
      const controls = groupedBy({...USAGE_CONTROLS_DEFAULT, within: first.value}, first.value);
      expect(controls.within).toBeNull();
    }
    let controls = USAGE_CONTROLS_DEFAULT;
    for (const kind of TOKEN_KINDS) controls = withKind(controls, kind.value);
    expect(controls.kinds).toHaveLength(1);
    expect(withKind(controls, controls.kinds[0] as TokenKind).kinds).toEqual(controls.kinds);
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
    inputGates: [],
    terminal: null,
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
    const view = usageView(usageRecords(state), USAGE_CONTROLS_DEFAULT, NO_CONTEXT);

    expect(headerView(state).spent?.title).toBe(view.tiles[1]?.title);
    expect(view.tiles[1]?.title).not.toBe('');
    expect(view.rows.at(-1)?.turns).toBe(String(fixture.records.length));
  });

  test('the header names what ran each role, from the records', () => {
    const agents = headerView(opened()).agents;

    expect(agents).toContainEqual({role: 'Planner', ran: ['gpt-6-luna on codex']});
  });

  test('a run folded from its events: the face totals say what the header says', () => {
    const state = opened();
    const core = reduceEventBatch(initialCoreState(), events);
    const folded = {...state, summary: {kind: 'idle'}, run: {...state.run, core}} as typeof state;
    const view = usageView(usageRecords(folded), USAGE_CONTROLS_DEFAULT, NO_CONTEXT);

    expect(headerView(folded).spent?.title).toBe(view.tiles[1]?.title);
    expect(usageRecords(folded)).toEqual(usageRecords(state));
  });
});
