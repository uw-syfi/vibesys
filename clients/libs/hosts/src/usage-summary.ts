/**
 * What a host answers the Usage page (its `UsageSummary` read) in the app's own terms: columnar
 * series of tokens and estimates per bucket, the distinct counts, the cheapest rows, how each
 * source was read, the price list's state and the person's price book. Nothing here derives a
 * cost: the host prices every row, and a client only adds the answers of several hosts.
 *
 * Times are in milliseconds. A bucket is a number: bucket `i` is `[edges[i], edges[i + 1])` of the
 * edges the page asked with, so the page cuts the days (in its own time zone) and every host
 * agrees on them.
 */

/** The longest list of projects or of edges the host accepts in one question. */
export const USAGE_SUMMARY_MAX_PROJECTS = 64;
export const USAGE_SUMMARY_MAX_EDGES = 100;
/** The last millisecond a host's calendar holds (the end of year 9999). */
const CALENDAR_END_MS = 253_402_300_799_000;
/** Edges are whole quarter hours: the grain a host rolls its runs up to. */
export const USAGE_QUARTER_MS = 900_000;

export type UsageSpeedName = 'standard' | 'fast' | 'ultrafast';
/** How a series' estimate was reached, in the order the host tries them. */
export type UsageEstimateSourceName = 'override' | 'reported' | 'recorded' | 'model' | 'unpriced';
export type UsageOrigin = 'run' | 'outside';
export type UsageSourceStatusName = 'ok' | 'missing' | 'partial' | 'failed';
export type UsagePricingStatusName = 'fresh' | 'cached' | 'bundled';

/** One series: a key and one value per populated bucket, in step with `buckets`. */
export interface UsageSeriesEntry {
  readonly origin: UsageOrigin;
  readonly provider: string;
  /** The model as the price list names it after `Map to`, or `other` for the folded remainder. */
  readonly model: string;
  readonly speed: UsageSpeedName;
  readonly source: UsageEstimateSourceName;
  /** The index in the answer's `sources` of the source the series was read from. */
  readonly usageSource: number;
  readonly buckets: readonly number[];
  /** Tokens read from the provider's cache. */
  readonly cached: readonly number[];
  readonly cacheWrite: readonly number[];
  readonly uncached: readonly number[];
  readonly output: readonly number[];
  /** The part of `output` the provider reports as reasoning. */
  readonly reasoning: readonly number[];
  readonly turns: readonly number[];
  /** Turns whose cost was reached from a record that predates the split by model. */
  readonly approximateTurns: readonly number[];
  readonly recordedUsd: readonly number[];
  readonly estimateUsd: readonly number[];
  readonly inputUsd: readonly number[];
  readonly cacheReadUsd: readonly number[];
  readonly cacheWriteUsd: readonly number[];
  readonly outputUsd: readonly number[];
  readonly speedPremiumUsd: readonly number[];
  readonly cacheSavingsUsd: readonly number[];
}

/** Distinct runs and sessions in one bucket for one provider: not summable across buckets. */
export interface UsageBucketCountEntry {
  readonly bucket: number;
  readonly provider: string;
  readonly runs: number;
  readonly outsideSessions: number;
}

export interface UsageProviderCostEntry {
  readonly provider: string;
  readonly estimateUsd: number;
  readonly recordedUsd: number;
  readonly tokens: number;
}

export interface UsageRunRowEntry {
  /** The index in the question's `projects` of the run's project. */
  readonly project: number;
  readonly runId: string;
  readonly costs: readonly UsageProviderCostEntry[];
}

export interface UsageAgentRowEntry {
  readonly role: string;
  readonly model: string;
  readonly costs: readonly UsageProviderCostEntry[];
}

/** One place the host read usage from, and how completely. */
export interface UsageSourceEntry {
  readonly kind: 'journal' | 'transcripts';
  /** The host's state directory identity: two answers naming one id describe one source. */
  readonly sourceId: string;
  /** Empty for the journals. */
  readonly provider: string;
  readonly root: string;
  readonly status: UsageSourceStatusName;
  readonly scanned: number;
  readonly skipped: number;
  readonly malformed: number;
  readonly failed: number;
  readonly distinctSessions: number;
  readonly excludedExact: number;
  readonly excludedApproximate: number;
}

/** US dollars per million tokens. */
export interface UsageRateEntry {
  readonly input: number;
  readonly output: number;
  readonly cacheRead: number;
  readonly cacheWrite: number;
  /** Null: the cache write rate applies to one hour writes too. */
  readonly cacheWrite1h: number | null;
}

export interface UsageResolvedRateEntry {
  readonly provider: string;
  readonly model: string;
  readonly speed: UsageSpeedName;
  /** Null: the model is unpriced. */
  readonly rate: UsageRateEntry | null;
  readonly source: UsageEstimateSourceName;
}

export interface UsagePricingEntry {
  readonly status: UsagePricingStatusName;
  readonly fetchedAt: number | null;
  readonly knownModels: number;
  /** Whether a refresh would fetch now. */
  readonly refreshDue: boolean;
  readonly checkedOn: string;
}

export interface UsageRateOverrideEntry {
  readonly model: string;
  readonly rate: UsageRateEntry;
}

export interface UsageAliasEntry {
  readonly from: string;
  readonly to: string;
}

/** What a person set about prices on one host; saved whole against `revision`. */
export interface UsagePriceBookEntry {
  readonly revision: number;
  readonly overrides: readonly UsageRateOverrideEntry[];
  readonly aliases: readonly UsageAliasEntry[];
}

/** The Usage page's figures from one host. */
export interface UsageSummaryEntry {
  readonly series: readonly UsageSeriesEntry[];
  readonly counts: readonly UsageBucketCountEntry[];
  /** Ideas that became the best so far, by the bucket they landed in. */
  readonly bests: readonly {readonly bucket: number; readonly count: number}[];
  readonly runs: readonly UsageRunRowEntry[];
  readonly runsOther: readonly UsageProviderCostEntry[];
  readonly agents: readonly UsageAgentRowEntry[];
  readonly agentsOther: readonly UsageProviderCostEntry[];
  /** The estimate of the last hour in whole quarter hours, by provider. */
  readonly recent: readonly UsageProviderCostEntry[];
  readonly sources: readonly UsageSourceEntry[];
  readonly pricing: UsagePricingEntry;
  readonly prices: UsagePriceBookEntry;
  /** `unreadable`: the host cannot use its stored prices, so `prices` is empty and not to be saved over. */
  readonly pricesStatus: 'ok' | 'unreadable';
  readonly rates: readonly UsageResolvedRateEntry[];
  /** Runs and transcript files the host left for a later call: ask again while either is above zero. */
  readonly pendingRuns: number;
  readonly pendingFiles: number;
  readonly foldedSeries: number;
  /** Resolved rates the host left out of `rates` because it lists at most 256. */
  readonly ratesOmitted: number;
  readonly readAt: number;
}

/** What a host answers a summary question: figures, or that its VibeSys offers none. */
export type UsageSummaryOutcome =
  | {readonly kind: 'summary'; readonly summary: UsageSummaryEntry}
  | {readonly kind: 'unsupported'};

/** What a host answers asking for fresh prices: how current its rates are after. */
export type UsageRatesOutcome =
  | {readonly kind: 'pricing'; readonly pricing: UsagePricingEntry}
  | {readonly kind: 'unsupported'};

/** What a host answers saving a price book. */
export type UsagePricesOutcome =
  | {readonly kind: 'saved'; readonly prices: UsagePriceBookEntry}
  /** The host's revision had moved: nothing changed, and this is its book. */
  | {readonly kind: 'moved'; readonly prices: UsagePriceBookEntry}
  | {readonly kind: 'unsupported'};

/**
 * Why a question cannot be asked of a host, as words; null when it can. The host refuses the same
 * questions, so asking one is a mistake of the caller.
 */
export function usageSummaryRefusal(
  projects: readonly string[],
  edges: readonly number[],
): string | null {
  if (projects.length > USAGE_SUMMARY_MAX_PROJECTS) {
    return `a usage question names at most ${USAGE_SUMMARY_MAX_PROJECTS} projects`;
  }
  if (edges.length < 2 || edges.length > USAGE_SUMMARY_MAX_EDGES) {
    return `a usage question has 2 to ${USAGE_SUMMARY_MAX_EDGES} edges`;
  }
  for (const [at, edge] of edges.entries()) {
    if (!Number.isInteger(edge) || edge % USAGE_QUARTER_MS !== 0) {
      return 'the edges of a usage question are whole quarter hours';
    }
    if (edge < 0 || edge > CALENDAR_END_MS) return 'an edge of a usage question is in the calendar';
    const before = edges[at - 1];
    if (before !== undefined && before >= edge) return 'the edges of a usage question increase';
  }
  return null;
}
