import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunOverview, Span} from '@vibesys/contracts';
import {event as fixtureEvent} from '@vibesys/contracts/testing';
import {type CoreState, initialCoreState, reduceEvent} from '@vibesys/run-state';
import {EMPTY_TRACE} from '@vibesys/run-state/trace';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {firstChart} from './chart-fixtures.js';
import {heroView} from './headline.js';
import {ideaOutcomeLine} from './idea-focus.js';
import {IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {traceLog} from './live.js';
import {NO_PAUSES} from './quota-pauses.js';
import {createRunReader, type RunFacts, type RunModel} from './run-model.js';
import {timelineRows} from './timeline-rows.js';

/**
 * A run whose input could not be measured by its own engine, so no idea has a ratio to it, but whose
 * ideas the framework compared with their bases. The gate spans state the reading of one run; the
 * comparison states the mean of every run the idea earned, and that mean is the idea's value.
 * Mocked event sequences: the spans an idea's gates label, and the comparisons the run folded.
 */

const T0 = Date.UTC(2026, 9, 18, 12, 0, 0) / 1000;
const METRIC = 'output_tokens_per_s';
const OVERVIEW: RunOverview = {
  overview_version: 1,
  objective: {text: 'Maximize throughput.', metric: METRIC, unit: 'tok/s', direction: 'max'},
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

/** One passed idea: its workstream, and the gate that read `sample` (the last run it earned). */
function idea(round: number, revision: string, sample: number, baseRound: number | null): Span[] {
  const rows = [{name: METRIC, value: sample, unit: 'tok/s', direction: 'max'}];
  const at = T0 + round * 1000;
  return [
    span(`flow:${round}/#1`, null, {
      started_at: at,
      ended_at: at + 900,
      label: {
        kind: 'workstream',
        title: `Idea ${round}`,
        verdict: 'passed',
        attrs_json: JSON.stringify({
          hypothesis_id: `h${round}`,
          round,
          revision,
          base_round: baseRound,
        }),
      },
    }),
    span(`op:${round}/measure.1`, `flow:${round}/#1`, {
      started_at: at + 400,
      ended_at: at + 800,
      label: {
        kind: 'gate',
        title: 'accuracy + benchmark',
        verdict: 'passed',
        attrs_json: JSON.stringify({
          stage: 'accuracy,benchmark',
          metric_value: sample,
          metrics: rows,
          input_metrics: [],
        }),
      },
    }),
  ];
}

function compared(
  sequence: number,
  candidate: string,
  base: string,
  values: {readonly candidate: number; readonly base: number},
  verdict: 'better' | 'within_noise' | 'worse' | 'inconclusive' = 'better',
): RunEvent {
  return fixtureEvent(sequence, 'measurement_compared', {
    data: {
      kind: 'measurement_compared',
      metric: METRIC,
      unit: 'tok/s',
      direction: 'max',
      candidate_commit: candidate,
      base_commit: base,
      candidate_value: values.candidate,
      base_value: values.base,
      ratio: values.candidate / values.base,
      noise: 0.02,
      verdict,
      candidate_samples: 2,
      base_samples: 2,
    },
  });
}

/** The spans of `spans` as the page folds them, read with the facts of `core`. */
function modelOf(spans: readonly Span[], core: CoreState, pauses = NO_PAUSES): RunModel {
  const stream = spans.map(
    (each, index) =>
      ({
        protocol_version: 1,
        run_id: 'run',
        sequence: index + 1,
        timestamp: new Date((T0 + 10 + index) * 1000).toISOString(),
        type: 'span_ended',
        data: {kind: 'span_ended', span: each},
      }) as RunEvent,
  );
  const facts: RunFacts = {comparisons: core.comparisons, baselines: core.baselines, pauses};
  return createRunReader().read(traceLog(EMPTY_TRACE, OVERVIEW, stream), null, null, facts);
}

const SPANS = [...idea(2, 'c2', 352.1, null), ...idea(3, 'c3', 395.4, 2)];
const CORE = [
  compared(1, 'c2', 'rin', {candidate: 350.39, base: 286.96}),
  compared(2, 'c3', 'c2', {candidate: 385.4, base: 350.4}),
].reduce(reduceEvent, initialCoreState());

describe('an idea whose measurement the framework compared', () => {
  const model = modelOf(SPANS, CORE);

  test('has the compared mean as its value, not the reading of the run that answered its gate', () => {
    expect(model.candidates.candidates.map(item => item.measurement?.metricValue)).toEqual([
      350.39, 385.4,
    ]);
    expect(model.candidates.candidates.map(item => item.measurement?.readings[0]?.value)).toEqual([
      350.39, 385.4,
    ]);
  });

  test('headlines the best idea’s compared mean with the ratio the comparison made, by its base', () => {
    const [tile] = heroView(model).headlines;
    expect(tile).toMatchObject({
      figure: '385.4',
      ratio: null,
      detail: '1.10× vs round 2',
      // The tooltip gives the means the ratio came from.
      hint: '385.4 vs 350.4 · 1.10× vs round 2',
    });
  });

  test('has that mean in the chart’s points, whose tooltips read it', () => {
    const chart = firstChart(model, T0, T0 + 4000);
    expect(chart.readings.map(point => point.y)).toEqual([350.39, 385.4]);
    expect(chart.readings.map(point => point.label)).toEqual([
      'Idea 2 · 350.4 tok/s',
      'Idea 3 · 385.4 tok/s',
    ]);
  });

  test('says the ratio it made on the Ideas list and the Timeline, not that there is no input', () => {
    // The Ideas column is narrow too: the base in a few characters, the whole cell on hover.
    const ideas = ideasView(model, IDEAS_FILTER_DEFAULT).rows;
    expect(ideas.map(row => [row.title, row.figure])).toEqual([
      ['Idea 3', '1.10× r2'],
      ['Idea 2', '1.22× base'],
    ]);
    expect(ideas.map(row => row.figureTitle)).toEqual(['1.10× vs round 2', '1.22× vs its base']);
    const clock = {start: T0, now: T0 + 4000};
    // The Timeline's column is narrow: the base in a few characters, the whole cell on hover.
    const rows = timelineRows(model, 'all', clock, false).rows;
    expect(rows.map(row => row.figure)).toEqual(['1.10× r2', '1.22× base']);
    expect(rows.map(row => row.figureTitle)).toEqual(['1.10× vs round 2', '1.22× vs its base']);
  });

  test('says the ratio it made in the open idea’s line, by its base', () => {
    const idea = model.candidates.candidates[1];
    expect(idea && ideaOutcomeLine(model, idea)).toContain('1.10× vs round 2 (385.4 tok/s)');
  });

  test('words the objective’s unit as the rest of the page does, never as its metric key', () => {
    const keyed = {
      ...model,
      overview: model.overview && {
        ...model.overview,
        objective: {...model.overview.objective, unit: 'output_tokens_per_s'},
      },
    };
    const idea = keyed.candidates.candidates[1];
    expect(idea && ideaOutcomeLine(keyed, idea)).toContain('385.4 output tokens/s');
    const ideas = ideasView(keyed, IDEAS_FILTER_DEFAULT);
    expect(JSON.stringify(ideas)).not.toContain('output_tokens_per_s');
  });

  test('has the ratio to the input from the mean, once the input was measured', () => {
    const measured = SPANS.map(each => {
      const attrs = JSON.parse(each.label?.attrs_json ?? 'null') as Record<string, unknown> | null;
      if (attrs === null || each.label?.kind !== 'gate') return each;
      const rows = attrs['metrics'] as {name: string}[];
      const input = [{...rows[0], value: 286.96}];
      return {
        ...each,
        label: {...each.label, attrs_json: JSON.stringify({...attrs, input_metrics: input})},
      };
    });
    const rows = ideasView(modelOf(measured, CORE), IDEAS_FILTER_DEFAULT).rows;
    expect(rows.map(row => row.figure)).toEqual(['1.34×', '1.22×']);
  });

  test('is the same object, and so are its ideas, while nothing it reads changes', () => {
    const reader = createRunReader();
    const log = traceLog(EMPTY_TRACE, OVERVIEW, []);
    const facts: RunFacts = {
      comparisons: CORE.comparisons,
      baselines: CORE.baselines,
      pauses: NO_PAUSES,
    };
    expect(reader.read(log, null, null, facts)).toBe(reader.read(log, null, null, facts));
  });

  test('is read from any comparison values: its value is the comparison’s, its ratio its own', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const mean = 1 + random.int(100_000) / 100;
      const base = 1 + random.int(100_000) / 100;
      const core = reduceEvent(
        initialCoreState(),
        compared(1, 'c2', 'rin', {candidate: mean, base}),
      );
      const [only] = modelOf(idea(2, 'c2', 1 + random.int(100_000) / 100, null), core).candidates
        .candidates;
      expect(only?.measurement?.metricValue, `seed ${seed}`).toBe(mean);
      expect(only?.measurement?.against?.ratio, `seed ${seed}`).toBe(mean / base);
    }
  });

  test('leaves an idea no comparison names as the gate read it', () => {
    const none = modelOf(SPANS, initialCoreState());
    expect(none.candidates.candidates.map(item => item.measurement?.metricValue)).toEqual([
      352.1, 395.4,
    ]);
    expect(none.candidates.candidates.map(item => item.measurement?.against)).toEqual([null, null]);
  });
});

describe('an idea whose comparison with its base was within noise', () => {
  const NOISY = [
    compared(1, 'c2', 'rin', {candidate: 350.39, base: 286.96}),
    compared(2, 'c3', 'c2', {candidate: 352.4, base: 350.4}, 'within_noise'),
  ].reduce(reduceEvent, initialCoreState());
  const model = modelOf(SPANS, NOISY);

  test('is no new best in the chart, though its value is higher', () => {
    const chart = firstChart(model, T0, T0 + 4000);
    expect(chart.readings.map(point => point.y)).toEqual([350.39, 352.4]);
    // The line holds the first idea's value to the end: the second is no step of it.
    expect(new Set(chart.steps.map(step => step.y))).toEqual(new Set([350.39]));
  });

  test('is no best on the Ideas list, from the same reading', () => {
    expect(model.candidates.best?.candidate).toBe(model.candidates.candidates[0]?.id);
  });

  test('is a new best when the comparison says better, for any pair of values', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const first = 1 + random.int(1000);
      const second = first + 1 + random.int(1000);
      for (const verdict of ['better', 'within_noise', 'worse', 'inconclusive'] as const) {
        const core = [
          compared(1, 'c2', 'rin', {candidate: first, base: 1}),
          compared(2, 'c3', 'c2', {candidate: second, base: first}, verdict),
        ].reduce(reduceEvent, initialCoreState());
        const chart = firstChart(modelOf(SPANS, core), T0, T0 + 4000);
        const reading = chart.readings[1];
        const sets = chart.steps.some(step => step.x === reading?.x && step.y === reading?.y);
        const expected = verdict === 'better' || verdict === 'inconclusive';
        expect(sets, `seed ${seed} ${verdict}`).toBe(expected);
      }
    }
  });
});
