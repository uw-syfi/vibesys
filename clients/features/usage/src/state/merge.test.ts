import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {claimSources, FIELDS, type HostReply, mergeReplies} from './merge.js';
import {repliesFor, summaryFor} from './testing/generate.js';

const N = 7;

/** Every field's total over the series of `replies`, summed from the raw columns. */
function totals(replies: readonly HostReply[]): Record<string, number> {
  const found: Record<string, number> = Object.fromEntries(FIELDS.map(field => [field, 0]));
  for (const reply of replies) {
    for (const series of reply.summary.series) {
      for (const field of FIELDS) {
        found[field] = (found[field] ?? 0) + series[field].reduce((a, b) => a + b, 0);
      }
    }
  }
  return found;
}

function mergedTotals(replies: readonly HostReply[]): Record<string, number> {
  const merged = mergeReplies(claimSources(replies), N);
  const found: Record<string, number> = Object.fromEntries(FIELDS.map(field => [field, 0]));
  for (const series of merged.series) {
    expect(series.values.estimateUsd).toHaveLength(N);
    for (const field of FIELDS) {
      found[field] = (found[field] ?? 0) + series.values[field].reduce((a, b) => a + b, 0);
    }
  }
  return found;
}

function shuffled<T>(rng: Seeded, items: readonly T[]): T[] {
  const copy = [...items];
  for (let at = copy.length - 1; at > 0; at -= 1) {
    const other = rng.int(at + 1);
    [copy[at], copy[other]] = [copy[other] as T, copy[at] as T];
  }
  return copy;
}

describe('mergeReplies', () => {
  test('property: hosts that read different sources add up, field by field, without loss', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const rng = new Seeded(seed);
      const replies = repliesFor(rng, N, 1 + rng.int(3));
      expect(mergedTotals(replies), `seed ${seed}`).toEqual(totals(replies));
    }
  });

  test('property: the order the hosts answered in does not change the sum', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const rng = new Seeded(seed);
      const replies = repliesFor(rng, N, 2 + rng.int(2));
      const once = mergeReplies(claimSources(replies), N);
      const again = mergeReplies(claimSources(shuffled(rng, replies)), N);
      expect(again, `seed ${seed}`).toEqual(once);
    }
  });

  test('property: a source that two hosts name counts once, whichever host read it', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const rng = new Seeded(seed);
      const [first] = repliesFor(rng, N, 1);
      if (first === undefined) throw new Error('no reply generated');
      // The same state directory seen from a second machine: same sources, same figures.
      const second: HostReply = {
        ...first,
        host: 'ssh:other',
        summary: {...first.summary, readAt: first.summary.readAt - 1},
      };
      const merged = mergeReplies(claimSources([first, second]), N);
      expect(mergedTotals([first, second]), `seed ${seed}`).toEqual(totals([first]));
      expect(merged.duplicates, `seed ${seed}`).toBe(first.summary.sources.length);
      expect(merged.distinct, `seed ${seed}`).toEqual(
        mergeReplies(claimSources([first]), N).distinct,
      );
    }
  });

  test('property: answers beyond the question’s buckets are left out, never wrapped around', () => {
    for (const seed of SEEDS.slice(0, 60)) {
      const rng = new Seeded(seed);
      const long = summaryFor(rng, N + 5, 'long');
      const merged = mergeReplies(
        claimSources([{host: 'local', projects: ['/p'], summary: long}]),
        N,
      );
      const inside = totals([
        {
          host: 'local',
          projects: [],
          summary: {
            ...long,
            series: long.series.map(series => {
              const keep = series.buckets.map(bucket => bucket < N);
              const pick = (column: readonly number[]) => column.filter((_, at) => keep[at]);
              return {
                ...series,
                buckets: pick(series.buckets),
                cached: pick(series.cached),
                cacheWrite: pick(series.cacheWrite),
                uncached: pick(series.uncached),
                output: pick(series.output),
                reasoning: pick(series.reasoning),
                turns: pick(series.turns),
                approximateTurns: pick(series.approximateTurns),
                recordedUsd: pick(series.recordedUsd),
                estimateUsd: pick(series.estimateUsd),
                inputUsd: pick(series.inputUsd),
                cacheReadUsd: pick(series.cacheReadUsd),
                cacheWriteUsd: pick(series.cacheWriteUsd),
                outputUsd: pick(series.outputUsd),
                speedPremiumUsd: pick(series.speedPremiumUsd),
                cacheSavingsUsd: pick(series.cacheSavingsUsd),
              };
            }),
          },
        },
      ]);
      const got: Record<string, number> = Object.fromEntries(FIELDS.map(field => [field, 0]));
      for (const series of merged.series) {
        for (const field of FIELDS) {
          got[field] = (got[field] ?? 0) + series.values[field].reduce((a, b) => a + b, 0);
        }
      }
      expect(got, `seed ${seed}`).toEqual(inside);
    }
  });
});
