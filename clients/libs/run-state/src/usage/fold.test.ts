import {describe, expect, it} from 'bun:test';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {
  foldUsage,
  NO_USAGE,
  totalUsage,
  USAGE_DIMENSIONS,
  type UsageDimension,
  usageOverTime,
} from './index.js';

/** mulberry32: a seeded generator (fast-check is not a dependency). */
class Seeded {
  #state: number;

  constructor(seed: number) {
    this.#state = seed >>> 0;
  }

  int(bound: number): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let value = this.#state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) % bound;
  }

  pick<T>(items: readonly T[]): T {
    return items[this.int(items.length)] as T;
  }
}

const ROLES = ['planner', 'implementer', 'judge', null] as const;
const HARNESSES = ['codex', 'claude', null] as const;
const MODELS = ['gpt-6-luna', 'sonnet-5', null] as const;
const RUNS = ['run-a', 'run-b'] as const;
const EPOCH = Date.parse('2026-10-01T00:00:00Z');

function count(random: Seeded): number | null {
  return random.int(4) === 0 ? null : random.int(1_000_000);
}

function record(random: Seeded, runs: readonly string[]): UsageRecord {
  const ended = random.int(6) === 0 ? null : new Date(EPOCH + random.int(48 * 3600) * 1000);
  return {
    run_id: random.pick(runs),
    execution_id: `exec-${random.int(9)}`,
    span_id: null,
    round_label: random.pick(['round-1', 'round-2', null]),
    role: random.pick(ROLES),
    harness: random.pick(HARNESSES),
    model: random.pick(MODELS),
    reasoning_effort: random.pick(['high', null]),
    client: null,
    harness_version: null,
    started_at: null,
    ended_at: ended === null ? null : ended.toISOString(),
    tokens: {
      input: count(random),
      cache_read: count(random),
      cache_write: count(random),
      output: count(random),
      reasoning: count(random),
    },
    cost_usd: random.int(3) === 0 ? null : random.int(5000) / 100,
    duration_ms: null,
  };
}

function records(seed: number, runs: readonly string[] = RUNS): UsageRecord[] {
  const random = new Seeded(seed);
  return Array.from({length: random.int(40)}, () => record(random, runs));
}

const SEEDS = Array.from({length: 80}, (_, index) => index + 1);
const GROUPINGS: readonly (readonly UsageDimension[])[] = [
  [],
  ...USAGE_DIMENSIONS.map(dimension => [dimension]),
  ['role', 'model'],
  ['run', 'harness'],
];

function sums(list: readonly {totals: ReturnType<typeof totalUsage>}[]) {
  return list.reduce(
    (a, b) => ({
      turns: a.turns + b.totals.turns,
      input: b.totals.tokens.input === null ? a.input : (a.input ?? 0) + b.totals.tokens.input,
      cost: b.totals.costUsd === null ? a.cost : (a.cost ?? 0) + b.totals.costUsd,
    }),
    {turns: 0, input: null as number | null, cost: null as number | null},
  );
}

describe('foldUsage', () => {
  it('totals nothing for no records, with every count unreported', () => {
    expect(foldUsage([], ['role'])).toEqual({groups: [], total: NO_USAGE});
  });

  it('puts every record in exactly one group, whatever the grouping', () => {
    for (const seed of SEEDS) {
      const list = records(seed);
      for (const by of GROUPINGS) {
        const fold = foldUsage(list, by);
        const grouped = sums(fold.groups);
        expect(grouped.turns).toBe(list.length);
        expect(fold.total.turns).toBe(list.length);
        expect(grouped.input).toBe(fold.total.tokens.input);
        expect(grouped.cost === null ? null : Math.round(grouped.cost * 100)).toBe(
          fold.total.costUsd === null ? null : Math.round(fold.total.costUsd * 100),
        );
      }
    }
  });

  it('keeps a value the journal does not hold apart from every real value', () => {
    const list = records(7).filter(item => item.role === null || item.role === 'planner');
    const keys = foldUsage(list, ['role']).groups.map(group => group.key[0]);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('folds the records of two runs as one set, the run being one more dimension', () => {
    for (const seed of SEEDS) {
      const a = records(seed, ['run-a']);
      const b = records(seed + 1000, ['run-b']);
      const both = foldUsage([...a, ...b], ['run']);
      expect(both.groups.map(group => group.key[0]).sort()).toEqual(
        [...new Set([...a, ...b].map(item => item.run_id))].sort(),
      );
      const ofA = both.groups.find(group => group.key[0] === 'run-a');
      expect(ofA?.totals ?? NO_USAGE).toEqual(totalUsage(a));
    }
  });

  it('is independent of the order records come in, for counts', () => {
    for (const seed of SEEDS) {
      const list = records(seed);
      const reversed = [...list].reverse();
      expect(foldUsage(reversed, ['role']).total.tokens).toEqual(
        foldUsage(list, ['role']).total.tokens,
      );
    }
  });
});

describe('grouping by idea', () => {
  const ideaOf = (record: UsageRecord): string | null =>
    record.execution_id === null ? null : `idea-${record.execution_id.length % 3}`;

  it('puts every turn in one idea, the turns of no idea together, and sums to the total', () => {
    for (const seed of SEEDS) {
      const list = records(seed);
      const fold = foldUsage(list, ['idea'], ideaOf);
      expect(fold.groups.reduce((sum, group) => sum + group.totals.turns, 0)).toBe(list.length);
      expect(fold.total).toEqual(totalUsage(list));
      const none = list.filter(record => ideaOf(record) === null).length;
      expect(fold.groups.find(group => group.key[0] === null)?.totals.turns ?? 0).toBe(none);
    }
  });
});

describe('usageOverTime', () => {
  it('loses no record to bucketing: buckets and the undated sum to the total', () => {
    for (const seed of SEEDS) {
      const list = records(seed);
      for (const count of [1, 5, 24]) {
        const series = usageOverTime(list, 'role', count);
        const turns =
          series.buckets.reduce((sum, bucket) => sum + bucket.total.turns, 0) +
          series.undated.turns;
        expect(turns).toBe(list.length);
        const input =
          series.buckets.reduce((sum, bucket) => sum + (bucket.total.tokens.input ?? 0), 0) +
          (series.undated.tokens.input ?? 0);
        expect(input).toBe(totalUsage(list).tokens.input ?? 0);
        for (const bucket of series.buckets) {
          expect(sums(bucket.groups).turns).toBe(bucket.total.turns);
        }
      }
    }
  });

  it('has no more buckets than asked for, each ending where the next begins', () => {
    for (const seed of SEEDS) {
      const series = usageOverTime(records(seed), 'model', 8);
      expect(series.buckets.length).toBeLessThanOrEqual(8);
      series.buckets.forEach((bucket, index) => {
        const next = series.buckets[index + 1];
        if (next !== undefined) expect(bucket.to).toBe(next.from);
      });
    }
  });

  it('puts records that all ended at one instant in one bucket', () => {
    const at = new Date(EPOCH).toISOString();
    const list = records(3).map(item => ({...item, ended_at: at}));
    const series = usageOverTime(list, 'role', 10);
    expect(series.buckets).toHaveLength(list.length === 0 ? 0 : 1);
  });
});
