/**
 * The sessions of a run, read from the epoch headers and the times its records state: one session
 * per start of the run, from the first time it recorded to the last, however long it stood stopped
 * between them, and at every prefix of the run.
 */
import {describe, expect, test} from 'bun:test';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton, seekSkeleton} from '@vibesys/run-skeleton';
import {clockThrough, readSkeleton} from '@vibesys/run-state/skeleton';
import fc from 'fast-check';
import {parts, type Records, recordsOf} from './session-records.js';

function sessionsAt(records: readonly Records[number][], through: number) {
  const skeleton = foldSkeleton(seedSkeleton(), records.slice(0, through));
  return readSkeleton(skeleton, seekSkeleton(skeleton, skeleton.throughSeq ?? 0n)).sessions;
}

describe('the sessions of a run', () => {
  for (const downtime of [false, true]) {
    test(`are one per start, each from its first time to its last (the clock ${downtime ? 'runs on' : 'stops'} while stopped)`, () => {
      fc.assert(
        fc.property(parts, each => {
          const {records, expected} = recordsOf(each, downtime);
          expect(sessionsAt(records, records.length)).toEqual(expected);
        }),
        {numRuns: 12},
      );
    });
  }

  test('at every prefix are the sessions of the whole run so far: only the last one grows', () => {
    fc.assert(
      fc.property(parts, each => {
        const {records, expected} = recordsOf(each, true);
        for (let length = 1; length <= records.length; length += 3) {
          const sessions = sessionsAt(records, length);
          expect(sessions.length).toBeLessThanOrEqual(expected.length);
          for (const [at, session] of sessions.entries()) {
            const whole = expected[at];
            expect(session.from).toBe(whole?.from as number);
            expect(session.to).toBeLessThanOrEqual(whole?.to as number);
            if (at < sessions.length - 1) expect(session.to).toBe(whole?.to as number);
          }
        }
      }),
      {numRuns: 12},
    );
  });

  test('a run started once is one session', () => {
    const {records} = storeRunFixture('short', 3, 1);
    expect(sessionsAt(records, records.length)).toHaveLength(1);
  });
});

describe('where the time bar stands on the run clock', () => {
  test('is the latest time stated by each position, never going back', () => {
    fc.assert(
      fc.property(parts, each => {
        const {records} = recordsOf(each, true);
        const skeleton = foldSkeleton(seedSkeleton(), records);
        const seqs = records.map((_, at) => at + 1);
        const times = clockThrough(skeleton, seqs);
        let reached = Number.NEGATIVE_INFINITY;
        for (const time of times) {
          if (time === null) continue;
          expect(time).toBeGreaterThanOrEqual(reached);
          reached = time;
        }
        const last = times.at(-1);
        expect(last).toBe(
          Math.max(
            ...records.flatMap(r =>
              r.body.case === 'span' ? [r.body.value.span?.endedAt ?? 0] : [0],
            ),
          ),
        );
      }),
      {numRuns: 10},
    );
  });
});
