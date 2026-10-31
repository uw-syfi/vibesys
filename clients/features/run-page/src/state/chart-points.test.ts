/**
 * The performance chart is drawn exactly when the run has measured points, at every prefix of every
 * generated history: one point is a chart too, and no point is no chart.
 */
import {expect, test} from 'bun:test';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {projectSkeleton} from './index.js';
import {heroView} from './headline.js';

test('the tile trend is drawn iff the history holds a measured point', () => {
  for (const kind of ['short', 'live'] as const) {
    for (const count of [1, 3]) {
      for (const seed of [1, 2]) {
        const {records} = storeRunFixture(kind, count, seed);
        for (let length = 1; length <= records.length; length += 3) {
          const skeleton = foldSkeleton(seedSkeleton(), records.slice(0, length));
          const points = skeleton.metrics.reduce((sum, series) => sum + series.points.length, 0);
          const [tile] = heroView(projectSkeleton(skeleton, skeleton.throughSeq ?? 0n)).headlines;
          expect(tile?.trend !== null && tile?.trend !== undefined).toBe(points > 0);
        }
      }
    }
  }
});
