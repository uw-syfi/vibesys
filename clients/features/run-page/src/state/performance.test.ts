import {describe, expect, test} from 'bun:test';
import type {Candidate, CandidateMeasurement} from '@vibesys/run-state/candidates';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {type PerformanceFold, type PerformancePoint, performanceFold} from './performance.js';
import type {Direction} from './run-model.js';
import {idea} from './timeline-fixtures.js';

/**
 * The performance chart's fold over generated runs: the best-so-far line only improves and is the
 * best passed value up to each time that beats the input; an idea sits on it exactly when it set a
 * new best; ideas that passed without beating it never rise above it, and a failed idea is never on
 * it. An idea's result is its own outcome (passed or failed), whatever its gate's verdict.
 */

type Verdict = 'passed' | 'failed_check' | 'slower' | null;
const VERDICTS: readonly Verdict[] = ['passed', 'passed', 'failed_check', 'slower', null];

function measured(item: Candidate, value: number, at: number, verdict: Verdict): Candidate {
  const measurement: CandidateMeasurement = {
    span: `${item.id}-g`,
    at,
    metricValue: value,
    vsBaseline: null,
    against: null,
    readings: [],
    inputReadings: [],
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
    const outcome = random.pick(['running', 'passed', 'failed'] as const);
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

function checkLine(
  points: readonly PerformancePoint[],
  direction: Direction,
  input: number | null,
): void {
  let best: number | null = input;
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
function checkBestUpTo(fold: PerformanceFold, direction: Direction, input: number | null): void {
  for (const point of fold.points) {
    const passed = fold.points
      .filter(each => each.result === 'passed' && each.at <= point.at)
      .map(each => each.value)
      .filter(value => input === null || better(value, input, direction));
    const line = fold.best.filter(step => step.at <= point.at).at(-1)?.value;
    const expected = direction === 'min' ? Math.min(...passed) : Math.max(...passed);
    expect(line).toBe(passed.length === 0 ? undefined : expected);
  }
}

/** An idea's result is its own outcome, passed or failed; pending while it runs. */
function checkResults(fold: PerformanceFold, items: readonly Candidate[]): void {
  for (const point of fold.points) {
    const outcome = items.find(each => each.id === point.id)?.outcome ?? 'running';
    expect(point.result).toBe(outcome === 'running' ? 'pending' : outcome);
  }
}

describe('the performance fold', () => {
  test('over any run the line improves, is the best passed value up to each time, and carries exactly the new bests', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const items = randomRun(random);
      const input = random.int(3) === 0 ? null : random.int(1000) / 8;
      for (const direction of DIRECTIONS) {
        const fold = performanceFold(items, direction, input);
        checkSteps(fold, direction);
        checkLine(fold.points, direction, input);
        checkBestUpTo(fold, direction, input);
        checkResults(fold, items);
      }
    }
  });

  test('a failed idea, however good its number, never sets the best; a slower passed idea is passed', () => {
    const fold = performanceFold(
      [
        measured(idea('a', 'passed', 0, 5), 10, 10, 'passed'),
        measured(idea('b', 'failed', 0, 5), 99, 20, 'failed_check'),
        measured(idea('c', 'passed', 0, 5), 9, 30, 'slower'),
        measured(idea('d', 'passed', 0, 5), 8, 40, 'passed'),
      ],
      'max',
      null,
    );
    expect(fold.points.map(point => [point.id, point.result, point.best])).toEqual([
      ['a', 'passed', true],
      ['b', 'failed', false],
      ['c', 'passed', false],
      ['d', 'passed', false],
    ]);
    expect(fold.best).toEqual([{at: 10, value: 10}]);
  });

  test('a passed idea slower than the input is grey: the input stands as the best', () => {
    const fold = performanceFold(
      [
        measured(idea('a', 'passed', 0, 5), 8, 10, 'slower'),
        measured(idea('b', 'passed', 0, 5), 12, 20, 'passed'),
      ],
      'max',
      10,
    );
    expect(fold.points.map(point => [point.id, point.result, point.best])).toEqual([
      ['a', 'passed', false],
      ['b', 'passed', true],
    ]);
    expect(fold.best).toEqual([{at: 20, value: 12}]);
  });

  test('an idea still running, with no verdict yet, is pending and not on the line', () => {
    const fold = performanceFold([measured(idea('a', 'running', 0, 5), 10, 10, null)], 'max', null);
    expect(fold.points.map(point => [point.result, point.best])).toEqual([['pending', false]]);
    expect(fold.best).toEqual([]);
  });

  test('ideas with nothing measured are not on the chart', () => {
    expect(performanceFold([idea('a', 'failed', 0, 5)], 'max', null)).toEqual({
      points: [],
      best: [],
    });
  });
});
