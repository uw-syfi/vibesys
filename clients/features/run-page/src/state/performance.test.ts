import {describe, expect, test} from 'bun:test';
import type {Candidate, CandidateMeasurement} from '@vibesys/run-state/candidates';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {type PerformanceFold, type PerformancePoint, performanceFold} from './performance.js';
import type {Direction} from './run-model.js';
import {idea} from './timeline-fixtures.js';

/**
 * The performance chart's fold over generated runs: the best-so-far line only improves and is the
 * best passed value up to each time; an idea sits on it exactly when it set a new best; ideas that
 * passed without beating it never rise above it, and a failed idea is never on it.
 */

type Verdict = 'passed' | 'failed_check' | 'slower' | null;
const VERDICTS: readonly Verdict[] = ['passed', 'passed', 'failed_check', 'slower', null];

function measured(item: Candidate, value: number, at: number, verdict: Verdict): Candidate {
  const measurement: CandidateMeasurement = {
    span: `${item.id}-g`,
    at,
    metricValue: value,
    vsBaseline: null,
    verdict,
    failed:
      verdict === 'failed_check'
        ? {check: 'accuracy', completed: 1, required: 2, unit: null}
        : null,
  };
  return {...item, measurement};
}

function randomRun(random: Seeded): Candidate[] {
  const count = random.int(40);
  return Array.from({length: count}, (_, at) => {
    const outcome = random.pick(['running', 'kept', 'rejected'] as const);
    const verdict = random.pick(VERDICTS);
    // Some ideas are measured out of the order they began, and some tie on time.
    const when = random.int(30) * 10;
    const item = idea(`i${at}`, outcome, at, 5);
    return random.int(6) === 0 ? item : measured(item, random.int(1000) / 8, when, verdict);
  });
}

function better(a: number, b: number, direction: Direction): boolean {
  return direction === 'min' ? a < b : a > b;
}

const DIRECTIONS: readonly Direction[] = ['max', 'min'];

function checkLine(points: readonly PerformancePoint[], direction: Direction): void {
  let best: number | null = null;
  for (const point of points) {
    if (point.result === 'passed' && (best === null || better(point.value, best, direction))) {
      expect(point.best).toBe(true);
      best = point.value;
    } else {
      expect(point.best).toBe(false);
      // A dot that is not on the line is never better than the line at that time.
      if (point.result === 'passed' && best !== null) {
        expect(better(point.value, best, direction)).toBe(false);
      }
    }
  }
}

function checkSteps(fold: PerformanceFold, direction: Direction): void {
  const times = fold.points.map(point => point.at);
  expect(times).toEqual([...times].sort((a, b) => a - b));
  const values = fold.best.map(step => step.value);
  values.slice(1).forEach((value, at) => {
    expect(better(value, values[at] as number, direction)).toBe(true);
  });
  expect(values).toEqual(fold.points.filter(point => point.best).map(point => point.value));
}

/** The line at each point's time is the best passed value measured up to that time. */
function checkBestUpTo(fold: PerformanceFold, direction: Direction): void {
  for (const point of fold.points) {
    const passed = fold.points
      .filter(each => each.result === 'passed' && each.at <= point.at)
      .map(each => each.value);
    const line = fold.best.filter(step => step.at <= point.at).at(-1)?.value;
    const expected = direction === 'min' ? Math.min(...passed) : Math.max(...passed);
    expect(line).toBe(passed.length === 0 ? undefined : expected);
  }
}

describe('the performance fold', () => {
  test('over any run the line improves, is the best passed value up to each time, and carries exactly the new bests', () => {
    for (const seed of SEEDS) {
      const items = randomRun(new Seeded(seed));
      for (const direction of DIRECTIONS) {
        const fold = performanceFold(items, direction);
        checkSteps(fold, direction);
        checkLine(fold.points, direction);
        checkBestUpTo(fold, direction);
      }
    }
  });

  test('a failed idea, however good its number, never sets the best', () => {
    const fold = performanceFold(
      [
        measured(idea('a', 'kept', 0, 5), 10, 10, 'passed'),
        measured(idea('b', 'rejected', 0, 5), 99, 20, 'failed_check'),
        measured(idea('c', 'kept', 0, 5), 99, 30, 'slower'),
        measured(idea('d', 'kept', 0, 5), 8, 40, 'passed'),
      ],
      'max',
    );
    expect(fold.points.map(point => [point.id, point.result, point.best])).toEqual([
      ['a', 'passed', true],
      ['b', 'failed', false],
      ['c', 'failed', false],
      ['d', 'passed', false],
    ]);
    expect(fold.best).toEqual([{at: 10, value: 10}]);
  });

  test('an idea still running, with no verdict yet, is pending and not on the line', () => {
    const fold = performanceFold([measured(idea('a', 'running', 0, 5), 10, 10, null)], 'max');
    expect(fold.points.map(point => [point.result, point.best])).toEqual([['pending', false]]);
    expect(fold.best).toEqual([]);
  });

  test('ideas with nothing measured are not on the chart', () => {
    expect(performanceFold([idea('a', 'rejected', 0, 5)], 'max')).toEqual({points: [], best: []});
  });
});
