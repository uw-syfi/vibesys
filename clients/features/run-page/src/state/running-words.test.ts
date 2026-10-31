/**
 * Every running-time figure of the Run screen is said by one formatter with one rounding rule: the
 * time bar's word at the end of a run and the caption under each chart's axis state the same total,
 * whatever the total is (a run of 21m 40s reads `21m 40s` in both, never `21m` and `22m`).
 */
import {describe, expect, test} from 'bun:test';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import fc from 'fast-check';
import {
  clockStart,
  objectiveCharts,
  projectSkeleton,
  runPageInitial,
  skeletonScreenState,
} from './index.js';
import {runningWords} from './running-time.js';
import {parts, recordsOf} from './session-records.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

describe('the words of a length of running time', () => {
  test('are floored to the second below an hour and to the minute from it', () => {
    expect(runningWords(0)).toBe('0s');
    expect(runningWords(59.9)).toBe('59s');
    expect(runningWords(60)).toBe('1m 00s');
    expect(runningWords(1300)).toBe('21m 40s');
    expect(runningWords(1319.9)).toBe('21m 59s');
    expect(runningWords(3599)).toBe('59m 59s');
    expect(runningWords(3600)).toBe('1h 00m');
    expect(runningWords(40677.9)).toBe('11h 17m');
  });

  test('never say a time later than the one given, and never go back as it grows', () => {
    fc.assert(
      fc.property(fc.double({min: 0, max: 400000, noNaN: true}), fc.nat(5000), (seconds, more) => {
        const words = (value: number): number => {
          const [hours, minutes, rest] = [
            /(\d+)h/.exec(runningWords(value))?.[1],
            /(\d+)m/.exec(runningWords(value))?.[1],
            /(\d+)s/.exec(runningWords(value))?.[1],
          ].map(part => Number(part ?? 0));
          return (hours ?? 0) * 3600 + (minutes ?? 0) * 60 + (rest ?? 0);
        };
        expect(words(seconds)).toBeLessThanOrEqual(seconds);
        expect(seconds - words(seconds)).toBeLessThan(seconds < 3600 ? 1 : 60);
        expect(words(seconds + more)).toBeGreaterThanOrEqual(words(seconds));
      }),
    );
  });
});

describe('the time bar and the charts at the end of a run', () => {
  test('state one total, whatever it is', () => {
    fc.assert(
      fc.property(parts, fc.boolean(), (each, downtime) => {
        const {records} = recordsOf(each, downtime);
        const skeleton = foldSkeleton(seedSkeleton(), records);
        const whole = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
        const state = skeletonScreenState(SEED, skeleton, whole, runPageInitial(SEED));
        const [chart] = objectiveCharts(whole, {start: clockStart(whole), now: null});
        expect(chart?.caption).toBe(`running time · ${state.positionWord}`);
      }),
      {numRuns: 40},
    );
  });
});
