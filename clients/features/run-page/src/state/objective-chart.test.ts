import {describe, expect, test} from 'bun:test';
import type {Candidate} from '@vibesys/run-state/candidates';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {openIdeaId} from './idea-focus.js';
import {objectiveCharts} from './objective-chart.js';
import type {RunModel} from './run-model.js';
import {idea, modelOf} from './timeline-fixtures.js';
import type {Route} from './zoom.js';

/**
 * The run level's performance charts as they draw: a y-axis from 0, the best-so-far line that steps
 * at each new best and reaches the end of the axis, a dot for every reading, and the open idea's
 * rings.
 */

function result(
  id: string,
  at: number,
  value: number,
  verdict: 'passed' | 'slower' | 'failed_check',
  vsBaseline: number | null = null,
): Candidate {
  const base = idea(id, verdict === 'passed' ? 'passed' : 'failed', at, 10);
  return {
    ...base,
    measurement: {
      span: `${id}-g`,
      at,
      metricValue: value,
      vsBaseline,
      against: null,
      readings: [],
      inputReadings: [],
      verdict,
      failed: null,
    },
  };
}

const chartOf = (
  model: RunModel,
  now: number | null,
  scrub: number | null = null,
  focus: string | null = null,
  lit: ReadonlySet<string> = new Set(),
) => objectiveCharts(model, {start: 0, now, scrub, focus, lit})[0];

describe('the performance chart', () => {
  test('draws every reading as a dot and steps the line at each new best', () => {
    const chart = chartOf(
      modelOf([
        result('a', 60, 10, 'passed'),
        result('b', 120, 8, 'passed'),
        result('c', 180, 50, 'slower'),
        result('d', 240, 14, 'passed'),
      ]),
      300,
    );
    expect(chart?.readings.map(point => point.id)).toEqual(['a', 'b', 'c', 'd']);
    // The line steps at the new bests and runs on to the end of the axis (5 minutes).
    expect(chart?.steps).toEqual([
      {x: 1, y: 10},
      {x: 4, y: 14},
      {x: 5, y: 14},
    ]);
    // A reading's whole name says when it was read, and whether it set a step of the line.
    expect(chart?.readings.find(point => point.id === 'b')?.label).toBe('b · 8 · 2m 00s');
    expect(chart?.readings.find(point => point.id === 'a')?.label).toBe(
      'a · 10 · 1m 00s · new best',
    );
  });

  test('the line starts at the input’s value at the start of the axis, and names it', () => {
    const chart = chartOf(
      modelOf([result('a', 60, 20, 'passed', 2), result('b', 120, 30, 'passed', 3)]),
      300,
    );
    expect(chart?.start).toBe('Input');
    expect(chart?.steps).toEqual([
      {x: 0, y: 10},
      {x: 1, y: 20},
      {x: 2, y: 30},
      {x: 5, y: 30},
    ]);
    expect(chart?.readings.find(point => point.id === 'b')?.label).toBe(
      'b · 30 · 3.00× · 2m 00s · new best',
    );
  });

  test('a reading’s tip is its idea and the short figures; its name for assistive technology is whole', () => {
    const chart = chartOf(
      modelOf([result('a', 60, 6_054_154, 'passed', 4), result('b', 120, 756_769, 'slower', 0.5)]),
      300,
    );
    const [first, second] = chart?.readings ?? [];
    expect(first?.tip).toEqual({lead: 'a', detail: '6.05M · 4.00×'});
    expect(first?.label).toBe('a · 6,054,154 · 4.00× · 1m 00s · new best');
    expect(second?.tip).toEqual({lead: 'b', detail: '757K · 0.50×'});
  });

  test('with no input there is no start to name', () => {
    const chart = chartOf(modelOf([result('a', 60, 20, 'passed')]), 300);
    expect(chart?.start).toBeNull();
  });

  test('the y-axis starts at 0 whatever was measured', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const ideas = Array.from({length: 1 + random.int(12)}, (_, at) =>
        result(`i${at}`, 60 * (at + 1), 1000 + random.int(9000), random.pick(['passed', 'slower'])),
      );
      const chart = chartOf(modelOf(ideas), null);
      const ticks = chart?.yTicks.map(tick => tick.value) ?? [];
      expect(Math.min(...ticks)).toBe(0);
      expect(Math.max(...ticks)).toBeGreaterThanOrEqual(0);
    }
  });

  test('before anything is measured the axes are drawn, empty', () => {
    const chart = chartOf(modelOf([idea('a', 'running', 0, 10)]), 5);
    expect(chart?.readings).toEqual([]);
    expect(chart?.steps).toEqual([]);
    // Silent axes: four gridlines over the whole height, none with a word, and the time axis.
    expect(chart?.yTicks.map(tick => tick.value)).toEqual([0.25, 0.5, 0.75, 1]);
    expect(chart?.yTicks.every(tick => tick.label === '')).toBe(true);
    expect(chart?.yDomain).toEqual([0, 1]);
    expect(chart?.xTicks.length).toBeGreaterThan(0);
  });

  test('the x axis reads minutes of the run, with its length as the caption', () => {
    const chart = chartOf(modelOf([result('a', 60, 10, 'passed')]), 25 * 60);
    expect(chart?.xTicks.map(tick => tick.label)).toEqual(['0m', '5m', '10m', '15m', '20m', '25m']);
    expect(chart?.caption).toBe('running time · 25m 00s');
    expect(chart?.resumes).toEqual([]);
  });
});

/** Measured ideas `i0…`, some built on one before, some not measured at all. */
function run(seed: number): Candidate[] {
  const random = new Seeded(seed);
  return Array.from({length: 2 + random.int(10)}, (_, at) => {
    const parent = at > 0 && random.int(2) === 0 ? `i${random.int(at)}` : null;
    const base =
      random.int(4) === 0
        ? idea(`i${at}`, 'running', 60 * (at + 1), 10)
        : result(
            `i${at}`,
            60 * (at + 1),
            1000 + random.int(9000),
            random.pick(['passed', 'slower'] as const),
          );
    return {...base, parent};
  });
}

describe('the chart with an idea open', () => {
  test('rings the open idea’s reading and no other, none removed', () => {
    const chart = chartOf(
      modelOf([
        result('a', 60, 10, 'passed'),
        {...result('b', 120, 8, 'passed'), parent: 'a'},
        result('c', 180, 12, 'passed'),
      ]),
      300,
      null,
      'b',
    );
    expect(chart?.readings.map(point => [point.id, point.selected])).toEqual([
      ['a', false],
      ['b', true],
      ['c', false],
    ]);
  });

  test('rings the steps its lineage set in the accent, and only steps', () => {
    const chart = chartOf(
      modelOf([
        result('a', 60, 10, 'passed'),
        {...result('b', 120, 14, 'passed'), parent: 'a'},
        result('c', 180, 12, 'passed'),
      ]),
      300,
      null,
      'b',
      new Set(['a', 'b', 'c']),
    );
    // `c` is on the chain but beat nothing: it set no step, so it has no ring.
    expect(chart?.readings.map(point => [point.id, point.lit])).toEqual([
      ['a', true],
      ['b', true],
      ['c', false],
    ]);
  });

  test('an open idea with no reading rings nothing', () => {
    const chart = chartOf(
      modelOf([result('a', 60, 10, 'passed'), idea('going', 'running', 100, 10)]),
      300,
      null,
      'going',
    );
    expect(chart?.readings.some(point => point.selected)).toBe(false);
  });

  test('with no open idea, or one the run lacks, nothing is ringed', () => {
    const model = modelOf([result('a', 60, 10, 'passed'), result('b', 120, 8, 'passed')]);
    for (const focus of [null, 'nobody']) {
      const chart = chartOf(model, 300, null, focus);
      expect(chart?.readings.every(point => !point.selected && !point.lit)).toBe(true);
    }
  });

  test('for any route, exactly the open idea’s reading is ringed, and the chart does not move', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const model = {
        ...modelOf(run(seed)),
        agents: {agents: []},
        trace: {spans: new Map()},
      } as unknown as RunModel;
      const ids = model.candidates.candidates.map(item => item.id);
      const route: Route = {
        mode: random.pick(['ideas', 'timeline', 'agents', 'logs'] as const),
        idea: random.pick([null, 'ghost', ...ids]),
        agent: random.pick([null, 'ghost']),
        step: random.pick([null, 'i0-a-g', 'ghost']),
        item: random.pick([null, 'x']),
      };
      const open = openIdeaId(model, route);
      const plain = chartOf(model, 3000);
      const chart = chartOf(model, 3000, null, open);
      const ringed = chart?.readings.filter(point => point.selected).map(point => point.id);
      const measured = plain?.readings.map(point => point.id) ?? [];
      expect(ringed).toEqual(open !== null && measured.includes(open) ? [open] : []);
      // Only how dots are drawn differs: where they are, the line, the ticks and both ranges are
      // the same with or without an idea open.
      const bare = (view: typeof chart) => ({
        ...view,
        readings: view?.readings.map(({selected: _, ...point}) => point),
      });
      expect(bare(chart)).toEqual(bare(plain));
    }
  });
});

describe('the chart scrubbed', () => {
  test('the cursor is the time bar’s position in minutes of running time; the axes keep the whole run', () => {
    const model = modelOf([
      result('a', 60, 10, 'passed'),
      result('b', 120, 20, 'passed'),
      result('c', 240, 30, 'passed'),
    ]);
    const live = chartOf(model, null);
    const scrubbed = chartOf(model, null, 150);
    expect(live?.cursor).toBeNull();
    expect(scrubbed?.cursor).toBe(2.5);
    const {cursor: _, ...rest} = scrubbed ?? {};
    const {cursor: __, ...whole} = live ?? {};
    expect(rest).toEqual(whole);
  });
});
