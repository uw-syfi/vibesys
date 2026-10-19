import {describe, expect, test} from 'bun:test';
import type {Candidate} from '@vibesys/run-state/candidates';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {openIdeaId} from './idea-focus.js';
import {stripChartView} from './run-level.js';
import type {RunModel} from './run-model.js';
import {idea, modelOf} from './timeline-fixtures.js';
import type {Route} from './zoom.js';

/**
 * The run level's performance chart as it draws: a y-axis from 0, the best-so-far step line that
 * reaches the end of the axis, and a tone per dot (on the line, passed but not better, failed).
 */

function result(
  id: string,
  at: number,
  value: number,
  verdict: 'passed' | 'slower' | 'failed_check',
): Candidate {
  const base = idea(id, verdict === 'passed' ? 'passed' : 'failed', at, 10);
  return {
    ...base,
    measurement: {
      span: `${id}-g`,
      at,
      metricValue: value,
      vsBaseline: null,
      against: null,
      readings: [],
      inputReadings: [],
      verdict,
      failed: null,
    },
  };
}

describe('the strip chart', () => {
  test('colors a new best on the line, a slower pass grey and a failure red', () => {
    const chart = stripChartView(
      modelOf([
        result('a', 60, 10, 'passed'),
        result('b', 120, 8, 'passed'),
        result('c', 180, 50, 'slower'),
        result('d', 240, 14, 'passed'),
      ]),
      0,
      300,
    );
    expect(chart?.points.map(point => [point.id, point.tone])).toEqual([
      ['a', 'accent'],
      ['b', 'muted'],
      ['c', 'err'],
      ['d', 'accent'],
    ]);
    // The line steps at the new bests and runs on to the end of the axis (5 minutes).
    expect(chart?.steps).toEqual([
      {x: 1, y: 10},
      {x: 4, y: 14},
      {x: 5, y: 14},
    ]);
    expect(chart?.points.find(point => point.id === 'b')?.label).toContain('Passed');
    expect(chart?.points.find(point => point.id === 'c')?.label).toContain('Failed');
    expect(chart?.points.find(point => point.id === 'a')?.label).toContain('New best');
  });

  test('the y-axis starts at 0 whatever was measured', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const ideas = Array.from({length: 1 + random.int(12)}, (_, at) =>
        result(`i${at}`, 60 * (at + 1), 1000 + random.int(9000), random.pick(['passed', 'slower'])),
      );
      const chart = stripChartView(modelOf(ideas), 0, null);
      const ticks = chart?.yTicks.map(tick => tick.value) ?? [];
      expect(Math.min(...ticks)).toBe(0);
      expect(Math.max(...ticks)).toBeGreaterThanOrEqual(0);
    }
  });

  test('draws nothing before anything is measured', () => {
    expect(stripChartView(modelOf([idea('a', 'running', 0, 10)]), 0, 5)).toBeNull();
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
  test('singles out the idea, outlines its base and dims the rest, none removed', () => {
    const chart = stripChartView(
      modelOf([
        result('a', 60, 10, 'passed'),
        {...result('b', 120, 8, 'passed'), parent: 'a'},
        result('c', 180, 12, 'passed'),
      ]),
      0,
      300,
      null,
      'b',
    );
    expect(chart?.points.map(point => [point.id, point.emphasis])).toEqual([
      ['a', 'related'],
      ['b', 'focus'],
      ['c', 'dim'],
    ]);
    // The idea's time span in minutes, as a band: it began at 120 s and settled 10 s in.
    expect(chart?.spans).toEqual([{from: 2, to: 130 / 60, label: 'b · 10s'}]);
  });

  test('an open idea with no dot dims nothing: there is nothing to single out', () => {
    const chart = stripChartView(
      modelOf([result('a', 60, 10, 'passed'), idea('going', 'running', 100, 10)]),
      0,
      300,
      null,
      'going',
    );
    expect(chart?.points.map(point => point.emphasis)).toEqual([undefined]);
    expect(chart?.spans).toHaveLength(1);
  });

  test('with no open idea, or one the run lacks, nothing is singled out and no band is drawn', () => {
    const model = modelOf([result('a', 60, 10, 'passed'), result('b', 120, 8, 'passed')]);
    for (const focus of [null, 'nobody']) {
      const chart = stripChartView(model, 0, 300, null, focus);
      expect(chart?.points.every(point => point.emphasis === undefined)).toBe(true);
      expect(chart?.spans).toEqual([]);
    }
  });

  test('for any route, exactly the open idea’s dots are the focus, and the chart does not move', () => {
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
      const plain = stripChartView(model, 0, 3000);
      const chart = stripChartView(model, 0, 3000, null, open);
      const focus = chart?.points
        .filter(point => point.emphasis === 'focus')
        .map(point => point.id);
      const measured = plain?.points.map(point => point.id) ?? [];
      expect(focus).toEqual(open !== null && measured.includes(open) ? [open] : []);
      // Only how dots are drawn differs: where they are, the line, the ticks and both ranges are
      // the same with or without an idea open.
      const bare = (view: typeof chart) => ({
        ...view,
        spans: [],
        points: view?.points.map(({emphasis: _, ...point}) => point),
      });
      expect(bare(chart)).toEqual(bare(plain));
    }
  });
});
