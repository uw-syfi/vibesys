/**
 * What several hosts answered the Usage page, added up into one set of series.
 *
 * Each host answers in the same buckets, so the answers add. Two things keep the sum honest:
 *
 * - **A source counts once.** A host's answer names the sources it read (its journals, each
 *   provider's transcripts) with the identity of the state directory behind them. Two profiles over
 *   one home, or two machines over one shared file system, name the same source: the best-read
 *   answer keeps it (`claimSources`) and the others drop their series from it and are counted.
 * - **The order of hosts does not matter.** Answers are added in host order, so the sum is the
 *   same whichever answered first.
 *
 * Every function is pure; the arrays are dense (one number per bucket) once merged.
 */
import type {HostKey, UsageSummaryEntry} from '@vibesys/hosts';

/** One host's answer, and the projects it was asked about (a run row names its project by index). */
export interface HostReply {
  readonly host: HostKey;
  readonly projects: readonly string[];
  readonly summary: UsageSummaryEntry;
}

/** A reply and the series of it that another answer already holds. */
export interface Claimed {
  readonly reply: HostReply;
  /** Indexes in `reply.summary.sources` whose series this reply gives up. */
  readonly dropped: ReadonlySet<number>;
}

const STATUS_RANK = {ok: 0, partial: 1, failed: 2, missing: 3} as const;

/** The sources of `replies` that more than one reply names, kept by the best-read answer. */
export function claimSources(replies: readonly HostReply[]): readonly Claimed[] {
  const ordered = [...replies].sort((a, b) => a.host.localeCompare(b.host));
  const best = new Map<string, {reply: HostReply; rank: number; read: number}>();
  for (const reply of ordered) {
    for (const source of reply.summary.sources) {
      const key = `${source.sourceId}|${source.kind}|${source.provider}`;
      const rank = STATUS_RANK[source.status];
      const held = best.get(key);
      if (
        held === undefined ||
        rank < held.rank ||
        (rank === held.rank && reply.summary.readAt > held.read)
      ) {
        best.set(key, {reply, rank, read: reply.summary.readAt});
      }
    }
  }
  return ordered.map(reply => ({
    reply,
    dropped: new Set(
      reply.summary.sources.flatMap((source, at) =>
        best.get(`${source.sourceId}|${source.kind}|${source.provider}`)?.reply === reply
          ? []
          : [at],
      ),
    ),
  }));
}

export const FIELDS = [
  'cached',
  'cacheWrite',
  'uncached',
  'output',
  'reasoning',
  'turns',
  'approximateTurns',
  'recordedUsd',
  'estimateUsd',
  'inputUsd',
  'cacheReadUsd',
  'cacheWriteUsd',
  'outputUsd',
  'speedPremiumUsd',
  'cacheSavingsUsd',
] as const;
export type Field = (typeof FIELDS)[number];

/** A series with one number per bucket for each field. */
export interface DenseSeries {
  readonly origin: 'run' | 'outside';
  readonly provider: string;
  readonly model: string;
  readonly speed: 'standard' | 'fast' | 'ultrafast';
  readonly source: 'override' | 'reported' | 'recorded' | 'model' | 'unpriced';
  readonly values: Readonly<Record<Field, readonly number[]>>;
}

export interface ProviderCost {
  readonly provider: string;
  readonly estimateUsd: number;
  readonly recordedUsd: number;
  readonly tokens: number;
}

interface RunCosts {
  readonly host: HostKey;
  readonly project: string;
  readonly runId: string;
  readonly costs: readonly ProviderCost[];
}

interface AgentCosts {
  readonly role: string;
  readonly model: string;
  readonly costs: readonly ProviderCost[];
}

export interface Merged {
  readonly buckets: number;
  readonly series: readonly DenseSeries[];
  /** Distinct runs and outside sessions by bucket, summed over providers. */
  readonly runs: readonly number[];
  readonly sessions: readonly number[];
  readonly bests: readonly number[];
  readonly runRows: readonly RunCosts[];
  readonly runsOther: readonly ProviderCost[];
  readonly agentRows: readonly AgentCosts[];
  readonly agentsOther: readonly ProviderCost[];
  readonly recent: readonly ProviderCost[];
  readonly sources: readonly {readonly host: HostKey; readonly index: number}[];
  /** Distinct runs the kept journals hold in the range, and distinct sessions the transcripts do. */
  readonly distinct: {readonly runs: number; readonly sessions: number};
  readonly duplicates: number;
  readonly pendingRuns: number;
  readonly pendingFiles: number;
  readonly foldedSeries: number;
}

/** The most runs and agents the page keeps after adding hosts: the host's own cuts, re-cut. */
export const MERGED_RUNS_MAX = 128;
export const MERGED_AGENTS_MAX = 64;

const zeros = (n: number): number[] => new Array<number>(n).fill(0);

function addCosts(into: Map<string, ProviderCost>, costs: readonly ProviderCost[]): void {
  for (const cost of costs) {
    const held = into.get(cost.provider);
    into.set(
      cost.provider,
      held === undefined
        ? cost
        : {
            provider: cost.provider,
            estimateUsd: held.estimateUsd + cost.estimateUsd,
            recordedUsd: held.recordedUsd + cost.recordedUsd,
            tokens: held.tokens + cost.tokens,
          },
    );
  }
}

const sortedCosts = (found: Map<string, ProviderCost>): ProviderCost[] =>
  [...found.values()].sort((a, b) => a.provider.localeCompare(b.provider));

const total = (costs: readonly ProviderCost[]): number =>
  costs.reduce((sum, cost) => sum + cost.estimateUsd, 0);

type Held = {key: Omit<DenseSeries, 'values'>; values: Record<Field, number[]>};

/** What the answers add up to so far. */
interface Sum {
  readonly buckets: number;
  readonly series: Map<string, Held>;
  readonly runs: number[];
  readonly sessions: number[];
  readonly bests: number[];
  readonly runRows: RunCosts[];
  readonly runsOther: Map<string, ProviderCost>;
  readonly agentRows: Map<string, {role: string; model: string; costs: Map<string, ProviderCost>}>;
  readonly agentsOther: Map<string, ProviderCost>;
  readonly recent: Map<string, ProviderCost>;
  readonly sources: {host: HostKey; index: number}[];
  duplicates: number;
  distinctRuns: number;
  distinctSessions: number;
  pendingRuns: number;
  pendingFiles: number;
  foldedSeries: number;
}

function emptySum(buckets: number): Sum {
  return {
    buckets,
    series: new Map(),
    runs: zeros(buckets),
    sessions: zeros(buckets),
    bests: zeros(buckets),
    runRows: [],
    runsOther: new Map(),
    agentRows: new Map(),
    agentsOther: new Map(),
    recent: new Map(),
    sources: [],
    duplicates: 0,
    distinctRuns: 0,
    distinctSessions: 0,
    pendingRuns: 0,
    pendingFiles: 0,
    foldedSeries: 0,
  };
}

function addSeries(found: Sum, summary: UsageSummaryEntry, dropped: ReadonlySet<number>): void {
  for (const each of summary.series) {
    if (dropped.has(each.usageSource)) continue;
    const key = [each.origin, each.provider, each.model, each.speed, each.source].join('\u0000');
    let held = found.series.get(key);
    if (held === undefined) {
      held = {
        key: {
          origin: each.origin,
          provider: each.provider,
          model: each.model,
          speed: each.speed,
          source: each.source,
        },
        values: Object.fromEntries(FIELDS.map(field => [field, zeros(found.buckets)])) as Record<
          Field,
          number[]
        >,
      };
      found.series.set(key, held);
    }
    each.buckets.forEach((bucket, at) => {
      if (bucket >= found.buckets) return;
      for (const field of FIELDS) {
        const values = held.values[field];
        values[bucket] = (values[bucket] ?? 0) + (each[field][at] ?? 0);
      }
    });
  }
}

/** Distinct counts and bests: a source another answer holds does not count again. */
function addCounts(found: Sum, summary: UsageSummaryEntry, dropped: ReadonlySet<number>): void {
  const journalDropped = summary.sources.some(
    (source, at) => source.kind === 'journal' && dropped.has(at),
  );
  const providersDropped = new Set(
    summary.sources.flatMap((source, at) =>
      source.kind === 'transcripts' && dropped.has(at) ? [source.provider] : [],
    ),
  );
  for (const count of summary.counts) {
    if (count.bucket >= found.buckets) continue;
    if (!journalDropped) found.runs[count.bucket] = (found.runs[count.bucket] ?? 0) + count.runs;
    if (!providersDropped.has(count.provider)) {
      found.sessions[count.bucket] = (found.sessions[count.bucket] ?? 0) + count.outsideSessions;
    }
  }
  for (const best of summary.bests) {
    if (best.bucket < found.buckets) {
      found.bests[best.bucket] = (found.bests[best.bucket] ?? 0) + best.count;
    }
  }
}

/** The rows by run and by agent; the runs of a journal another answer holds fold into the rest. */
function addRows(found: Sum, reply: HostReply, dropped: ReadonlySet<number>): void {
  const {summary} = reply;
  addCosts(found.recent, summary.recent);
  addCosts(found.runsOther, summary.runsOther);
  if (summary.sources.some((source, at) => source.kind === 'journal' && dropped.has(at))) {
    addCosts(
      found.runsOther,
      summary.runs.flatMap(row => row.costs),
    );
    return;
  }
  for (const row of summary.runs) {
    found.runRows.push({
      host: reply.host,
      project: reply.projects[row.project] ?? '',
      runId: row.runId,
      costs: row.costs,
    });
  }
  for (const agent of summary.agents) {
    const keyed = `${agent.role}\u0000${agent.model}`;
    const held = found.agentRows.get(keyed) ?? {
      role: agent.role,
      model: agent.model,
      costs: new Map(),
    };
    addCosts(held.costs, agent.costs);
    found.agentRows.set(keyed, held);
  }
  addCosts(found.agentsOther, summary.agentsOther);
}

function addReply(found: Sum, {reply, dropped}: Claimed): void {
  const {summary} = reply;
  found.duplicates += dropped.size;
  found.pendingRuns += summary.pendingRuns;
  found.pendingFiles += summary.pendingFiles;
  found.foldedSeries += summary.foldedSeries;
  summary.sources.forEach((source, index) => {
    if (dropped.has(index)) return;
    found.sources.push({host: reply.host, index});
    if (source.kind === 'journal') found.distinctRuns += source.distinctSessions;
    else found.distinctSessions += source.distinctSessions;
  });
  addSeries(found, summary, dropped);
  addCounts(found, summary, dropped);
  addRows(found, reply, dropped);
}

/** The answers added: series by key, the rows re-cut to the page's bounds, the leftovers summed. */
export function mergeReplies(claimed: readonly Claimed[], buckets: number): Merged {
  const found = emptySum(buckets);
  for (const each of claimed) addReply(found, each);
  const rankedRuns = found.runRows.sort(
    (a, b) =>
      total(b.costs) - total(a.costs) || `${a.host}${a.runId}`.localeCompare(`${b.host}${b.runId}`),
  );
  for (const cut of rankedRuns.slice(MERGED_RUNS_MAX)) addCosts(found.runsOther, cut.costs);
  const rankedAgents = [...found.agentRows.values()]
    .map(each => ({role: each.role, model: each.model, costs: sortedCosts(each.costs)}))
    .sort(
      (a, b) =>
        total(b.costs) - total(a.costs) ||
        `${a.role}${a.model}`.localeCompare(`${b.role}${b.model}`),
    );
  for (const cut of rankedAgents.slice(MERGED_AGENTS_MAX)) addCosts(found.agentsOther, cut.costs);
  return {
    buckets,
    series: [...found.series.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([, each]) => ({...each.key, values: each.values})),
    runs: found.runs,
    sessions: found.sessions,
    bests: found.bests,
    runRows: rankedRuns.slice(0, MERGED_RUNS_MAX),
    runsOther: sortedCosts(found.runsOther),
    agentRows: rankedAgents.slice(0, MERGED_AGENTS_MAX),
    agentsOther: sortedCosts(found.agentsOther),
    recent: sortedCosts(found.recent),
    sources: found.sources,
    distinct: {runs: found.distinctRuns, sessions: found.distinctSessions},
    duplicates: found.duplicates,
    pendingRuns: found.pendingRuns,
    pendingFiles: found.pendingFiles,
    foldedSeries: found.foldedSeries,
  };
}
