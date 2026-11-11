import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {LATEST_COUNT, latestMeasurements} from './latest.js';
import {reportsOf, searchOf, servedSeries} from './served-points.js';

describe('the latest measurements of a report', () => {
  it('lists at most the newest eight jobs, newest first, one row for each job, whatever the objectives', () => {
    fc.assert(
      fc.property(servedSeries, series => {
        const objectives = reportsOf(searchOf(series));
        const rows = latestMeasurements(objectives, undefined);
        const all = new Map(objectives.flatMap(o => o.points.map(p => [p.id, p.atMs] as const)));
        expect(rows).toHaveLength(Math.min(LATEST_COUNT, all.size));
        expect(new Set(rows.map(row => row.id)).size).toBe(rows.length);
        rows.forEach((row, at) => {
          if (at > 0) expect(row.atMs).toBeLessThanOrEqual(rows[at - 1]?.atMs ?? 0);
        });
        // Nothing left out is newer than anything listed.
        const oldest = rows.at(-1)?.atMs ?? 0;
        const listed = new Set(rows.map(row => row.id));
        for (const [id, at] of all) if (!listed.has(id)) expect(at).toBeLessThanOrEqual(oldest);
      }),
      {seed: 51},
    );
  });

  it('has one value for each objective in their order, the value and the new-best mark exactly as the host served them', () => {
    fc.assert(
      fc.property(servedSeries, series => {
        const objectives = reportsOf(searchOf(series));
        for (const row of latestMeasurements(objectives, undefined, 100)) {
          expect(row.values).toHaveLength(objectives.length);
          objectives.forEach((objective, at) => {
            const served = objective.points.find(p => p.id === row.id);
            expect(row.values[at]).toEqual(
              served === undefined ? null : {value: served.value, newBest: served.bestSoFar},
            );
          });
        }
      }),
      {seed: 52},
    );
  });

  it('names the plans a job names, none for a job of no plan, and nothing while no ideas are served', () => {
    const objectives = reportsOf(
      searchOf([
        {
          objective: 'tok_s',
          direction: 1,
          points: [
            {jobId: 'a', commit: 'c1', at: {seconds: 10n}, value: 1, bestSoFar: true},
            {jobId: 'b', commit: 'c2', at: {seconds: 20n}, value: 2, bestSoFar: true},
          ],
        },
      ]),
    );
    const plans = new Map([
      ['a', [] as number[]],
      ['b', [7, 9]],
    ]);
    expect(latestMeasurements(objectives, plans).map(row => row.plans)).toEqual([[7, 9], []]);
    expect(latestMeasurements(objectives, undefined).map(row => row.plans)).toEqual([
      undefined,
      undefined,
    ]);
  });
});
