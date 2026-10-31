/** The Usage page's reads and its one change, through the gateway's registered operations.

`UsageSummary` cuts a host's usage into the buckets the page asks for; `UsageRatesRefresh` asks the
host to fetch its price list if one is due; `UsagePricesSave` replaces the prices a person set on
that host, against the revision they were read at. None depends on a helper connection. */
import {type Timestamp, timestampMs} from '@bufbuild/protobuf/wkt';
import type {
  HostId,
  UsageAliasEntry,
  UsageEstimateSourceName,
  UsagePriceBookEntry,
  UsagePricesOutcome,
  UsagePricingEntry,
  UsageRateEntry,
  UsageRatesOutcome,
  UsageSeriesEntry,
  UsageSourceEntry,
  UsageSourceStatusName,
  UsageSpeedName,
  UsageSummaryEntry,
  UsageSummaryOutcome,
} from '@vibesys/hosts';
import {
  acceptUsagePricesSave,
  acceptUsageRatesRefresh,
  acceptUsageSummary,
  callUsagePricesSave,
  callUsageRatesRefresh,
  callUsageSummary,
  decodeHostMutate,
  encodeHostMutate,
  type OperationSignal,
  operationPolicies,
  UsageEstimateSource,
  UsageOrigin,
  type UsagePriceBook,
  UsagePricesStatus,
  UsagePricingStatus,
  type UsageRate,
  UsageSourceKind,
  UsageSourceStatus,
  UsageSpeed,
  type UsageSummaryResponse,
} from '@vibesys/proto';
import {newRequestId} from '@vibesys/request-identity';
import {type RegistryReadPorts, registryTransport, requireCheckout} from './registry-reads.js';

export interface RegistryUsage {
  /** The host's usage cut into the buckets between `edges` (milliseconds, whole quarter hours). */
  summary(
    host: HostId,
    projects: readonly string[],
    edges: readonly number[],
    now: number,
    signal: AbortSignal,
  ): Promise<UsageSummaryOutcome>;
  /** Ask the host to fetch its price list if one is due. */
  refreshRates(
    host: HostId,
    force: boolean,
    now: number,
    signal: AbortSignal,
  ): Promise<UsageRatesOutcome>;
  /** Replace the host's price book, edited from `baseRevision`. */
  savePrices(
    host: HostId,
    baseRevision: number,
    overrides: readonly {readonly model: string; readonly rate: UsageRateEntry}[],
    aliases: readonly UsageAliasEntry[],
    signal: AbortSignal,
  ): Promise<UsagePricesOutcome>;
}

const SPEEDS: ReadonlyMap<UsageSpeed, UsageSpeedName> = new Map([
  [UsageSpeed.STANDARD, 'standard'],
  [UsageSpeed.FAST, 'fast'],
  [UsageSpeed.ULTRAFAST, 'ultrafast'],
]);

const SOURCES: ReadonlyMap<UsageEstimateSource, UsageEstimateSourceName> = new Map([
  [UsageEstimateSource.OVERRIDE, 'override'],
  [UsageEstimateSource.REPORTED, 'reported'],
  [UsageEstimateSource.RECORDED, 'recorded'],
  [UsageEstimateSource.MODEL, 'model'],
  [UsageEstimateSource.UNPRICED, 'unpriced'],
]);

const STATUSES: ReadonlyMap<UsageSourceStatus, UsageSourceStatusName> = new Map([
  [UsageSourceStatus.OK, 'ok'],
  [UsageSourceStatus.MISSING, 'missing'],
  [UsageSourceStatus.PARTIAL, 'partial'],
  [UsageSourceStatus.FAILED, 'failed'],
]);

const PRICING: ReadonlyMap<UsagePricingStatus, UsagePricingEntry['status']> = new Map([
  [UsagePricingStatus.FRESH, 'fresh'],
  [UsagePricingStatus.CACHED, 'cached'],
  [UsagePricingStatus.BUNDLED, 'bundled'],
]);

function named<K, V>(table: ReadonlyMap<K, V>, key: K, what: string): V {
  const found = table.get(key);
  if (found === undefined) throw new Error(`the host sent an unknown ${what}: ${String(key)}`);
  return found;
}

const ms = (stamp: Timestamp | undefined): number | null =>
  stamp === undefined ? null : timestampMs(stamp);

const count = (value: bigint): number => Number(value);

function rateEntry(rate: UsageRate): UsageRateEntry {
  return {
    input: rate.input,
    output: rate.output,
    cacheRead: rate.cacheRead,
    cacheWrite: rate.cacheWrite,
    cacheWrite1h: rate.cacheWrite1h ?? null,
  };
}

function pricingEntry(pricing: NonNullable<UsageSummaryResponse['pricing']>): UsagePricingEntry {
  return {
    status: named(PRICING, pricing.status, 'pricing status'),
    fetchedAt: ms(pricing.fetchedAt),
    knownModels: pricing.knownModels,
    refreshDue: pricing.refreshDue,
    checkedOn: pricing.checkedOn,
  };
}

function bookEntry(book: UsagePriceBook): UsagePriceBookEntry {
  return {
    revision: count(book.revision),
    overrides: book.overrides.flatMap(each =>
      each.rate === undefined ? [] : [{model: each.model, rate: rateEntry(each.rate)}],
    ),
    aliases: book.aliases.map(each => ({from: each.from, to: each.to})),
  };
}

function seriesEntry(series: UsageSummaryResponse['series'][number]): UsageSeriesEntry {
  const key = series.key;
  if (key === undefined) throw new Error('the host sent a usage series without a key');
  return {
    origin: key.origin === UsageOrigin.OUTSIDE_RUNS ? 'outside' : 'run',
    provider: key.provider,
    model: key.model,
    speed: named(SPEEDS, key.speed, 'speed'),
    source: named(SOURCES, key.source, 'estimate source'),
    usageSource: key.usageSource,
    buckets: [...series.bucket],
    cached: series.cached.map(count),
    cacheWrite: series.cacheWrite.map(count),
    uncached: series.uncached.map(count),
    output: series.output.map(count),
    reasoning: series.reasoning.map(count),
    turns: [...series.turns],
    approximateTurns: [...series.approximateTurns],
    recordedUsd: [...series.recordedUsd],
    estimateUsd: [...series.estimateUsd],
    inputUsd: [...series.inputUsd],
    cacheReadUsd: [...series.cacheReadUsd],
    cacheWriteUsd: [...series.cacheWriteUsd],
    outputUsd: [...series.outputUsd],
    speedPremiumUsd: [...series.speedPremiumUsd],
    cacheSavingsUsd: [...series.cacheSavingsUsd],
  };
}

function sourceEntry(source: UsageSummaryResponse['sources'][number]): UsageSourceEntry {
  return {
    kind: source.kind === UsageSourceKind.TRANSCRIPTS ? 'transcripts' : 'journal',
    sourceId: source.sourceId,
    provider: source.provider,
    root: source.root,
    status: named(STATUSES, source.status, 'source status'),
    scanned: source.scanned,
    skipped: source.skipped,
    malformed: source.malformed,
    failed: source.failed,
    distinctSessions: source.distinctSessions,
    excludedExact: source.excludedExact,
    excludedApproximate: source.excludedApproximate,
  };
}

const costs = (rows: UsageSummaryResponse['runsOther']) =>
  rows.map(row => ({
    provider: row.provider,
    estimateUsd: row.estimateUsd,
    recordedUsd: row.recordedUsd,
    tokens: count(row.tokens),
  }));

/** What a usage summary response says, in the app's terms. */
export function usageSummaryEntry(response: UsageSummaryResponse): UsageSummaryEntry {
  const pricing = response.pricing;
  const prices = response.prices;
  if (pricing === undefined || prices === undefined) {
    throw new Error('the host sent a usage summary without its prices');
  }
  return {
    series: response.series.map(seriesEntry),
    counts: response.counts.map(each => ({
      bucket: each.bucket,
      provider: each.provider,
      runs: each.runs,
      outsideSessions: each.outsideSessions,
    })),
    bests: response.bests.map(each => ({bucket: each.bucket, count: each.count})),
    runs: response.runs.map(run => ({
      project: run.project,
      runId: run.runId,
      costs: costs(run.costs),
    })),
    runsOther: costs(response.runsOther),
    agents: response.agents.map(agent => ({
      role: agent.role,
      model: agent.model,
      costs: costs(agent.costs),
    })),
    agentsOther: costs(response.agentsOther),
    recent: costs(response.recent),
    sources: response.sources.map(sourceEntry),
    pricing: pricingEntry(pricing),
    prices: bookEntry(prices),
    pricesStatus: response.pricesStatus === UsagePricesStatus.UNREADABLE ? 'unreadable' : 'ok',
    rates: response.rates.map(each => ({
      provider: each.provider,
      model: each.model,
      speed: named(SPEEDS, each.speed, 'speed'),
      rate: each.rate === undefined ? null : rateEntry(each.rate),
      source: named(SOURCES, each.source, 'estimate source'),
    })),
    pendingRuns: response.pendingRuns,
    pendingFiles: response.pendingFiles,
    foldedSeries: response.foldedSeries,
    ratesOmitted: response.ratesOmitted,
    readAt: ms(response.readAt) ?? 0,
  };
}

const stamp = (millis: number) => ({
  seconds: BigInt(Math.floor(millis / 1000)),
  nanos: (((millis % 1000) + 1000) % 1000) * 1_000_000,
});

export interface RegistryUsagePorts extends RegistryReadPorts {
  /** Send one mutation envelope to the host's gateway. */
  mutate(host: HostId, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array>;
}

export function registryUsage(ports: RegistryUsagePorts): RegistryUsage {
  async function reads(host: HostId) {
    const checkout = requireCheckout(await ports.checkout(host), null);
    return registryTransport(checkout, (bytes, owned) => ports.unary(host, bytes, owned));
  }
  async function mutations(host: HostId, signal: AbortSignal) {
    const checkout = requireCheckout(await ports.checkout(host), null);
    if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
    return {
      async invoke(id: number, payload: Uint8Array, _totalMs: number, owned: OperationSignal) {
        if (!(owned instanceof AbortSignal)) throw new Error('operation cancelled');
        const policy = Object.values(operationPolicies).find(item => item.id === id);
        if (policy === undefined) throw new Error('unknown mutation');
        return decodeHostMutate(
          policy.name,
          await ports.mutate(host, encodeHostMutate(checkout, policy.name, payload), owned),
        );
      },
    };
  }
  return {
    async summary(host, projects, edges, now, signal) {
      const response = await callUsageSummary(
        await reads(host),
        acceptUsageSummary({
          requestId: newRequestId(),
          projects: [...projects],
          edges: edges.map(stamp),
          now: stamp(now),
        }),
        signal,
      );
      return {kind: 'summary', summary: usageSummaryEntry(response)};
    },
    async refreshRates(host, force, now, signal) {
      const response = await callUsageRatesRefresh(
        await reads(host),
        acceptUsageRatesRefresh({requestId: newRequestId(), force, now: stamp(now)}),
        signal,
      );
      if (response.pricing === undefined) throw new Error('the host sent no pricing');
      return {kind: 'pricing', pricing: pricingEntry(response.pricing)};
    },
    async savePrices(host, baseRevision, overrides, aliases, signal) {
      const response = await callUsagePricesSave(
        await mutations(host, signal),
        acceptUsagePricesSave({
          requestId: newRequestId(),
          baseRevision: BigInt(baseRevision),
          overrides: overrides.map(each => ({
            model: each.model,
            rate: {
              input: each.rate.input,
              output: each.rate.output,
              cacheRead: each.rate.cacheRead,
              cacheWrite: each.rate.cacheWrite,
              ...(each.rate.cacheWrite1h === null ? {} : {cacheWrite1h: each.rate.cacheWrite1h}),
            },
          })),
          aliases: aliases.map(each => ({from: each.from, to: each.to})),
        }),
        signal,
      );
      if (response.prices === undefined) throw new Error('the host sent no price book');
      const prices = bookEntry(response.prices);
      return response.saved ? {kind: 'saved', prices} : {kind: 'moved', prices};
    },
  };
}
