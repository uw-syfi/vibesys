import {describe, expect, test} from 'bun:test';
import type {Candidate} from '@vibesys/run-state/candidates';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {stripChartView} from './run-level.js';
import {idea, modelOf} from './timeline-fixtures.js';

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
  const base = idea(id, verdict === 'passed' ? 'kept' : 'rejected', at, 10);
  return {
    ...base,
    measurement: {
      span: `${id}-g`,
      at,
      metricValue: value,
      vsBaseline: null,
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
    expect(chart?.points.find(point => point.id === 'b')?.label).toContain('passed');
    expect(chart?.points.find(point => point.id === 'c')?.label).toContain('failed');
    expect(chart?.points.find(point => point.id === 'a')?.label).toContain('new best');
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
