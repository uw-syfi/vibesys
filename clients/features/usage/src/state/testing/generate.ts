/**
 * Generators for the state's tests: host answers drawn from the contract a host keeps (sparse,
 * increasing buckets; columns in step with them; whole token counts; dollars in multiples of
 * 1/1024 so sums are exact in any order), not from the folds under test.
 */
import type {HostKey, UsageSeriesEntry, UsageSummaryEntry} from '@vibesys/hosts';
import type {HostReply} from '../merge.js';

/** The draws a generator needs; the tests' seeded generator has them. */
export interface Random {
  int(bound: number): number;
  bool(): boolean;
  pick<T>(items: readonly T[]): T;
}

export const PROVIDERS = ['claude', 'codex', 'gemini'] as const;
const MODELS = ['model-a', 'model-b', 'model-c'] as const;
const SPEEDS = ['standard', 'fast'] as const;
const ESTIMATES = ['model', 'unpriced', 'override', 'recorded'] as const;

const dollars = (rng: Random): number => rng.int(40_000) / 1024;
const count = (rng: Random): number => rng.int(5_000_000);

/** The buckets a series is populated in: a sorted subset of `0..n`. */
function populated(rng: Random, n: number): number[] {
  return Array.from({length: n}, (_, at) => at).filter(() => rng.bool());
}

function seriesFor(
  rng: Random,
  n: number,
  origin: 'run' | 'outside',
  usageSource: number,
): UsageSeriesEntry {
  const buckets = populated(rng, n);
  const column = (make: (rng: Random) => number): number[] => buckets.map(() => make(rng));
  return {
    origin,
    provider: rng.pick(PROVIDERS),
    model: rng.pick(MODELS),
    speed: rng.pick(SPEEDS),
    source: rng.pick(ESTIMATES),
    usageSource,
    buckets,
    cached: column(count),
    cacheWrite: column(count),
    uncached: column(count),
    output: column(count),
    reasoning: column(count),
    turns: column(r => r.int(30)),
    approximateTurns: column(r => r.int(3)),
    recordedUsd: column(dollars),
    estimateUsd: column(dollars),
    inputUsd: column(dollars),
    cacheReadUsd: column(dollars),
    cacheWriteUsd: column(dollars),
    outputUsd: column(dollars),
    speedPremiumUsd: column(dollars),
    cacheSavingsUsd: column(dollars),
  };
}

/**
 * One host's answer over `n` buckets. Its sources are named `tag`-journal and `tag`-transcripts, so
 * two answers with different tags read different sources, and two with one tag read the same.
 */
export function summaryFor(rng: Random, n: number, tag: string): UsageSummaryEntry {
  const source = (kind: 'journal' | 'transcripts') => ({
    kind,
    sourceId: `${tag}-${kind}`,
    provider: kind === 'journal' ? '' : 'claude',
    root: '',
    status: 'ok' as const,
    scanned: 1,
    skipped: 0,
    malformed: 0,
    failed: 0,
    distinctSessions: 1 + rng.int(9),
    excludedExact: 0,
    excludedApproximate: 0,
  });
  const series = Array.from({length: 1 + rng.int(5)}, () => {
    const outside = rng.bool();
    return seriesFor(rng, n, outside ? 'outside' : 'run', outside ? 1 : 0);
  });
  const costs = () => [
    {
      provider: rng.pick(PROVIDERS),
      estimateUsd: dollars(rng),
      recordedUsd: dollars(rng),
      tokens: count(rng),
    },
  ];
  return {
    series,
    counts: Array.from({length: n}, (_, bucket) => ({
      bucket,
      provider: rng.pick(PROVIDERS),
      runs: rng.int(4),
      outsideSessions: rng.int(4),
    })),
    bests: Array.from({length: n}, (_, bucket) => ({bucket, count: rng.int(3)})),
    runs: Array.from({length: rng.int(6)}, (_, at) => ({
      project: 0,
      runId: `${tag}-run-${at}`,
      costs: costs(),
    })),
    runsOther: rng.bool() ? costs() : [],
    agents: Array.from({length: rng.int(4)}, (_, at) => ({
      role: `role-${at}`,
      model: rng.pick(MODELS),
      costs: costs(),
    })),
    agentsOther: [],
    recent: costs(),
    sources: [source('journal'), source('transcripts')],
    pricing: {status: 'fresh', fetchedAt: 1, knownModels: 3, refreshDue: false, checkedOn: ''},
    prices: {revision: 0, overrides: [], aliases: []},
    pricesStatus: 'ok',
    rates: [],
    pendingRuns: 0,
    pendingFiles: 0,
    foldedSeries: 0,
    readAt: 1 + rng.int(1000),
  };
}

/** Replies from `hosts` distinct hosts, each reading its own sources. */
export function repliesFor(rng: Random, n: number, hosts: number): readonly HostReply[] {
  return Array.from({length: hosts}, (_, at): HostReply => {
    const host: HostKey = at === 0 ? 'local' : `ssh:host-${at}`;
    return {host, projects: ['/p'], summary: summaryFor(rng, n, host)};
  });
}
