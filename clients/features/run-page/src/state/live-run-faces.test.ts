import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunOverview, RunSnapshot, Span} from '@vibesys/contracts';
import {EMPTY_TRACE} from '@vibesys/run-state/trace';
import {Seeded} from '@vibesys/store/testing';
import {IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {traceLog} from './live.js';
import {attached, events, failedGate, isoAt, opened, RUN, step, T0} from './live-run-fixtures.js';
import {heroView} from './headline.js';
import {createRunReader, type RunModel} from './run-model.js';
import type {SummaryReply} from './run-summary.js';
import {formatValue} from './span-reading.js';
import {timelineRows} from './timeline-rows.js';

/**
 * A live run whose input could not be measured: its input's gates failed before the first idea,
 * so its ideas have a value but no multiple over the input. Regression: the tile headlined a dash
 * and captioned the raw metric key, the Ideas list said only a dash, and the failed input gates
 * showed on no live run (a summary's gates were dropped once the session folded the run, and the
 * session's snapshot replaced the gates a summary had brought).
 */

const OVERVIEW: RunOverview = {
  overview_version: 1,
  objective: {
    text: 'Maximize throughput.',
    metric: 'output_tokens_per_s',
    unit: 'output_tokens_per_s',
    direction: 'max',
  },
  benchmarks: {},
};

function span(id: string, parent: string | null, extra: Partial<Span>): Span {
  return {
    id,
    parent,
    type: id.startsWith('op:') ? 'operation' : 'flow',
    name: id,
    status: 'done',
    started_at: T0 + 10,
    ended_at: T0 + 100,
    ...extra,
  } as Span;
}

interface Reading {
  readonly name: string;
  readonly value: number;
}

/** The spans of one passed idea whose benchmark read `readings` (the first is the headline). */
function passedIdea(
  readings: readonly Reading[],
  unit: string,
  baseline: {readonly input: number} | null,
): Span[] {
  const rows = readings.map(row => ({...row, unit, direction: 'max'}));
  const head = readings[0]?.value ?? 0;
  return [
    span('flow:1/#1', null, {
      label: {
        kind: 'workstream',
        title: 'Try a ring buffer',
        verdict: 'passed',
        attrs_json: JSON.stringify({hypothesis_id: 'h1', round: 1, revision: 'c1'}),
      },
    }),
    span('op:1/measure.1', 'flow:1/#1', {
      label: {
        kind: 'gate',
        title: 'accuracy + benchmark',
        verdict: 'passed',
        attrs_json: JSON.stringify({
          stage: 'accuracy,benchmark',
          metric_value: head,
          ...(baseline === null ? {} : {vs_baseline: head / baseline.input}),
          metrics: rows,
          input_metrics: baseline === null ? [] : [{...rows[0], value: baseline.input}],
        }),
      },
    }),
  ];
}

function modelOf(spans: readonly Span[], gates: readonly RunEvent[] = []): RunModel {
  const stream = spans.map(
    (each, index) =>
      ({
        protocol_version: 1,
        run_id: RUN.runId,
        sequence: gates.length + index + 1,
        timestamp: isoAt(T0 + 10 + index),
        type: 'span_ended',
        data: {kind: 'span_ended', span: each},
      }) as RunEvent,
  );
  return createRunReader().read(traceLog(EMPTY_TRACE, OVERVIEW, [...gates, ...stream]), null);
}

const TWO_GATES = [
  failedGate(1, 'accuracy', 'Error: no such module'),
  failedGate(2, 'benchmark', 'Error: no such module'),
];

describe('the performance tile of a run with no measured input', () => {
  const spans = passedIdea(
    [
      {name: 'total_tokens_per_s', value: 285.4},
      {name: 'output_tokens_per_s', value: 287.03},
    ],
    'output_tokens_per_s',
    null,
  );

  test('headlines the value of the objective’s own reading, with the unit in words', () => {
    const [tile] = heroView(modelOf(spans, TWO_GATES)).headlines;
    expect(tile).toMatchObject({
      name: 'Output tokens/s',
      ratio: null,
      figure: '287',
      note: 'output tokens/s',
      detail: 'input not measured',
    });
  });

  test('says there is no input when nothing failed either', () => {
    expect(heroView(modelOf(spans)).headlines[0]?.detail).toBe('no input');
  });

  test('draws the one value instead of nothing', () => {
    const trend = heroView(modelOf(spans, TWO_GATES)).headlines[0]?.trend;
    expect(trend?.steps).toHaveLength(1);
  });

  test('headlines any value as the screen writes numbers, and never shows a raw metric key', () => {
    const random = new Seeded(5);
    for (let round = 0; round < 100; round += 1) {
      const value = Math.round(random.next() * 10 ** random.int(7) * 1000) / 1000;
      const unit = random.pick(['output_tokens_per_s', 'ops/s', 'ms', 'p99_latency_ms']);
      const model = modelOf(passedIdea([{name: 'output_tokens_per_s', value}], unit, null));
      const [tile] = heroView(model).headlines;
      expect(tile?.figure).toBe(formatValue(value));
      expect(tile?.note ?? '').not.toContain('_');
      expect(tile?.detail).not.toContain('_');
    }
  });

  test('keeps the multiple as the headline once the input is measured', () => {
    const measured = passedIdea([{name: 'output_tokens_per_s', value: 300}], 'tok/s', {
      input: 150,
    });
    expect(heroView(modelOf(measured)).headlines[0]).toMatchObject({
      ratio: '2.00×',
      figure: '2.00×',
      note: 'faster than input',
    });
  });
});

describe('the vs input column of a run with no measured input', () => {
  const model = modelOf(passedIdea([{name: 'output_tokens_per_s', value: 287.03}], 'tok/s', null));

  test('says no input on the Ideas list and the Timeline, not a dash', () => {
    expect(ideasView(model, IDEAS_FILTER_DEFAULT).rows.map(row => row.figure)).toEqual([
      'no input',
    ]);
    const clock = {start: T0, now: T0 + 200};
    expect(timelineRows(model, 'all', clock, false).rows.map(row => row.figure)).toEqual([
      'no input',
    ]);
  });

  test('says the multiple once the input is measured', () => {
    const measured = modelOf(
      passedIdea([{name: 'output_tokens_per_s', value: 300}], 'tok/s', {input: 150}),
    );
    expect(ideasView(measured, IDEAS_FILTER_DEFAULT).rows.map(row => row.figure)).toEqual([
      '2.00×',
    ]);
  });
});

describe('the input gates that failed, on a live run', () => {
  const snapshot: RunSnapshot = {
    run_id: RUN.runId,
    sequence: 40,
    status: 'running',
    spans: passedIdea([{name: 'output_tokens_per_s', value: 287.03}], 'tok/s', null),
  };
  const reply: SummaryReply = {
    kind: 'summary',
    snapshot,
    seed: [],
    through: 40,
    eventCount: 40,
    journalBytes: 0,
    trace: 'recorded',
    roundsFinished: 0,
    records: [],
    gates: TWO_GATES,
    terminal: null,
  };

  function gatesOf(state: ReturnType<typeof attached>): string[] {
    return createRunReader()
      .read(state.run.trace, null)
      .inputGates.map(gate => gate.gate);
  }

  test('show when the session’s snapshot arrives before the summary', () => {
    const page = opened();
    const state = step(page.state, [
      page.attached,
      {type: 'run-data', wallMs: 0, data: {kind: 'snapshot', snapshot}},
      {type: 'summary', token: page.summaryToken, reply, wallMs: 1},
    ]);
    expect(gatesOf(state)).toEqual(['accuracy', 'benchmark']);
  });

  test('show when the summary arrives first and the session’s snapshot replaces its trace', () => {
    const page = opened();
    const state = step(page.state, [
      {type: 'summary', token: page.summaryToken, reply, wallMs: 0},
      page.attached,
      {type: 'run-data', wallMs: 1, data: {kind: 'snapshot', snapshot}},
    ]);
    expect(gatesOf(state)).toEqual(['accuracy', 'benchmark']);
  });

  test('stay once each through a replay of the run and a later snapshot', () => {
    const page = opened();
    const state = step(page.state, [
      page.attached,
      {type: 'run-data', wallMs: 0, data: {kind: 'snapshot', snapshot}},
      {type: 'summary', token: page.summaryToken, reply, wallMs: 1},
      {
        type: 'run-data',
        wallMs: 2,
        data: {kind: 'rebootstrap', base: 'preserve', events: [...TWO_GATES]},
      },
      events(3, [failedGate(2, 'benchmark', 'Error: no such module')]),
      {
        type: 'run-data',
        wallMs: 4,
        data: {kind: 'snapshot', snapshot: {...snapshot, sequence: 60}},
      },
    ]);
    expect(gatesOf(state)).toEqual(['accuracy', 'benchmark']);
  });

  test('read a later passing attempt the same, whichever of summary, snapshot and event came first', () => {
    const pass = {
      ...failedGate(45, 'benchmark', ''),
      status: 'completed',
    } as RunEvent;
    const standing = (state: ReturnType<typeof attached>): (number | null)[] =>
      createRunReader()
        .read(state.run.trace, null)
        .inputGates.map(gate => gate.supersededBy);
    const page = opened();
    const summary = {type: 'summary', token: page.summaryToken, reply, wallMs: 3} as const;
    const snap = {type: 'run-data', wallMs: 1, data: {kind: 'snapshot', snapshot}} as const;
    const live = events(2, [pass]);
    const orders = [
      [page.attached, snap, live, summary],
      [page.attached, snap, summary, live],
      [summary, page.attached, snap, live],
      [page.attached, live, snap, summary],
    ];
    for (const order of orders) {
      // The accuracy gate has no later pass and stands; the benchmark gate was passed at 45.
      expect(standing(step(page.state, order))).toEqual([null, 45]);
    }
    const rebooted = step(page.state, [
      page.attached,
      snap,
      summary,
      {type: 'run-data', wallMs: 4, data: {kind: 'rebootstrap', base: 'preserve', events: [pass]}},
    ]);
    expect(standing(rebooted)).toEqual([null, 45]);
  });

  test('never show on an attached run that has none', () => {
    const state = step(attached(), [
      {type: 'run-data', wallMs: 0, data: {kind: 'snapshot', snapshot}},
    ]);
    expect(gatesOf(state)).toEqual([]);
  });
});
