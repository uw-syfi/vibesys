/**
 * The performance chart draws a dot for every measured point, at every prefix of every generated
 * history: one point is a dot too, and no point is none.
 */
import {expect, test} from 'bun:test';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {firstChart} from './chart-fixtures.js';
import {projectSkeleton} from './index.js';

test('the chart holds a reading for every measured point of the history', () => {
  for (const kind of ['short', 'live'] as const) {
    for (const count of [1, 3]) {
      for (const seed of [1, 2]) {
        const {records} = storeRunFixture(kind, count, seed);
        for (let length = 1; length <= records.length; length += 3) {
          const skeleton = foldSkeleton(seedSkeleton(), records.slice(0, length));
          const points = skeleton.metrics.reduce((sum, series) => sum + series.points.length, 0);
          const chart = firstChart(projectSkeleton(skeleton, skeleton.throughSeq ?? 0n), 0, null);
          expect(chart.readings).toHaveLength(points);
        }
      }
    }
  }
});
