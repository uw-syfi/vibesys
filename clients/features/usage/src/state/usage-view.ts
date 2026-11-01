/**
 * What the Usage page shows, folded from the series the hosts answered: the hero figure, the
 * provider rows and the chart, the totals and their shares, the breakdown by model and by hour or
 * day, and what only VibeSys has (cost by run, by role, per new best and per hour now).
 *
 * Every figure is a sum of the answers' numbers, so rows add up to the figure that says what they
 * spent, and a count the answers could not place (tokens with no price, turns whose cost is a
 * guess, a price list that is not fresh) is carried as a chip, never dropped. Hiding a provider or
 * choosing a source changes a total by exactly the rows it leaves out. Nothing here prices
 * anything: the hosts priced every row.
 */
import {roleName} from '@vibesys/contracts';
import {type HostKey, hostFromKey, hostLabel, type UsagePricingEntry} from '@vibesys/hosts';
import type {PeriodEdges} from './edges.js';
import {bucketLabel} from './edges.js';
import {dollars, percent, plainName, tokens, turnsWords} from './format.js';
import type {DenseSeries, Field, Merged, ProviderCost} from './merge.js';

/** What the page measures; `limits` is the tab of plan limits, which no figure of the period feeds. */
export type UsageTab = 'cost' | 'tokens' | 'limits';
export type UsageSourceChoice = 'all' | 'run' | 'outside';

/** What the person chose to look at; the same answers, filtered. */
export interface ViewFilters {
  readonly tab: UsageTab;
  readonly source: UsageSourceChoice;
  /** Providers whose rows are left out. */
  readonly hidden: ReadonlySet<string>;
}

/** How a provider and its model are named, as the machine's catalogue gives them. */
export interface ModelName {
  readonly harness: string;
  readonly model: string;
  /** The catalogue's name for the provider; null when it gives none. */
  readonly harnessName: string | null;
  readonly modelName: string | null;
}

export interface ProviderRow {
  readonly provider: string;
  readonly name: string;
  /** The palette slot of the provider's mark, stable across filters. */
  readonly series: number;
  /** The provider's total, or `Unpriced` when no price covers any of its tokens. */
  readonly figure: string;
  /** Its share of the figure above, from 0 to 1. */
  readonly share: number;
  readonly hidden: boolean;
}

export interface ChartLayer {
  readonly provider: string;
  readonly name: string;
  readonly series: number;
  readonly values: readonly number[];
}

export interface Part {
  readonly key: string;
  readonly label: string;
  readonly value: number;
  readonly figure: string;
}

export interface Tile {
  readonly name: string;
  readonly figure: string;
  readonly detail: string | null;
}

export interface ModelRow {
  readonly key: string;
  readonly provider: string;
  readonly providerName: string;
  /** The model as the price list names it, else the catalogue's name for it. */
  readonly id: string;
  readonly name: string;
  readonly costUsd: number;
  /** What it cost, or `Unpriced` when no price covers any of its tokens. */
  readonly cost: string;
  readonly tokens: number;
  readonly tokensFigure: string;
  readonly turns: number;
  /** Dollars per million processed tokens; null when nothing was priced. */
  readonly perMillion: string | null;
  /** Cached input as a share of the input, from 0 to 1; null without input. */
  readonly cacheHit: number | null;
  readonly unpricedTokens: number;
  readonly share: number;
  /** The row's figure in each bucket, for its trend. */
  readonly trend: readonly number[];
  readonly overridden: boolean;
}

export interface TimeRow {
  readonly key: string;
  readonly label: string;
  readonly cost: string;
  readonly tokens: string;
  readonly turns: number;
  readonly runs: number;
  readonly sessions: number;
}

export interface RunRow {
  readonly key: string;
  readonly host: HostKey;
  readonly project: string;
  readonly runId: string;
  readonly costUsd: number;
  readonly cost: string;
  readonly share: number;
}

export interface AgentRow {
  readonly key: string;
  readonly role: string;
  /** The model as the price list names it. */
  readonly model: string;
  /** The catalogue's name for it, else its id in words. */
  readonly modelName: string;
  readonly cost: string;
  readonly share: number;
}

export interface ChipView {
  readonly key: string;
  readonly label: string;
  readonly hint: string | null;
  readonly tone: 'warn' | 'neutral';
}

export interface UsageView {
  readonly empty: boolean;
  readonly tab: UsageTab;
  /** The hero figure: what was spent (estimate) or processed (tokens). */
  readonly hero: {readonly figure: string; readonly caption: string};
  readonly chips: readonly ChipView[];
  readonly providers: readonly ProviderRow[];
  readonly chart: {
    readonly unit: string;
    readonly labels: readonly string[];
    readonly layers: readonly ChartLayer[];
  };
  readonly tiles: readonly Tile[];
  readonly costByKind: readonly Part[];
  readonly bySpeed: readonly Part[];
  readonly tokensByKind: readonly Part[];
  readonly models: readonly ModelRow[];
  readonly times: readonly TimeRow[];
  readonly runs: readonly RunRow[];
  readonly runsOther: string | null;
  readonly outside: string | null;
  readonly agents: readonly AgentRow[];
  readonly perNewBest: string | null;
  readonly newBests: number;
  readonly perHourNow: string;
  readonly unpricedModels: readonly string[];
}

const PALETTE = 8;

function sum(values: readonly number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

function column(series: readonly DenseSeries[], field: Field, buckets: number): number[] {
  const found = new Array<number>(buckets).fill(0);
  for (const each of series) {
    each.values[field].forEach((value, at) => {
      found[at] = (found[at] ?? 0) + value;
    });
  }
  return found;
}

function processed(each: DenseSeries): readonly number[] {
  return each.values.cached.map(
    (cached, at) =>
      cached +
      (each.values.cacheWrite[at] ?? 0) +
      (each.values.uncached[at] ?? 0) +
      (each.values.output[at] ?? 0),
  );
}

/** Tokens processed in each bucket, over `series`. */
function volumeOf(series: readonly DenseSeries[], buckets: number): number[] {
  return series.reduce<number[]>(
    (found, each) => processed(each).map((value, at) => value + (found[at] ?? 0)),
    new Array<number>(buckets).fill(0),
  );
}

/** The provider's name: the catalogue's if it has one, else its id in words. */
export function providerName(names: readonly ModelName[], provider: string): string {
  return names.find(each => each.harness === provider)?.harnessName ?? plainName(provider);
}

/** The model's name: the catalogue's if it has one, else its id in words. */
export function modelName(names: readonly ModelName[], provider: string, model: string): string {
  return (
    names.find(each => each.harness === provider && each.model === model)?.modelName ??
    plainName(model)
  );
}

/** Providers in a fixed order so a provider keeps its color whatever is hidden. */
export function providersOf(merged: Merged): readonly string[] {
  return [...new Set(merged.series.map(each => each.provider))].sort();
}

/** What a view is cut from: the merged answers, what the person chose, and the rows that remain. */
interface Cut {
  readonly merged: Merged;
  readonly filters: ViewFilters;
  readonly names: readonly ModelName[];
  readonly buckets: number;
  /** The series the source and the hidden providers leave. */
  readonly shown: readonly DenseSeries[];
  /** The palette slot of each provider, over all of them. */
  readonly palette: ReadonlyMap<string, number>;
}

const byTab = (tab: UsageTab, cost: number, count: number): number =>
  tab === 'cost' ? cost : count;

/** What a cost reads as when no price covers any of what was used: never a zero. */
const UNPRICED = 'Unpriced';

/** Whether tokens were used and none of them has a price. */
const uncovered = (used: number, unpriced: number): boolean => used > 0 && unpriced >= used;

function providerRows(cut: Cut, total: number): ProviderRow[] {
  const {merged, filters, names, palette} = cut;
  // Over every provider, so a hidden one can be turned back on.
  const held = new Map<string, {cost: number; tokens: number; unpriced: number}>();
  for (const each of merged.series) {
    if (filters.source !== 'all' && each.origin !== filters.source) continue;
    const own = held.get(each.provider) ?? {cost: 0, tokens: 0, unpriced: 0};
    own.cost += sum(each.values.estimateUsd);
    own.tokens += sum(processed(each));
    if (each.source === 'unpriced') own.unpriced += sum(processed(each));
    held.set(each.provider, own);
  }
  return [...held.entries()]
    .map(([provider, own]) => {
      const hidden = filters.hidden.has(provider);
      return {
        provider,
        name: providerName(names, provider),
        series: palette.get(provider) ?? 0,
        figure:
          filters.tab === 'cost'
            ? uncovered(own.tokens, own.unpriced)
              ? UNPRICED
              : dollars(own.cost)
            : tokens(own.tokens),
        share: hidden || total === 0 ? 0 : byTab(filters.tab, own.cost, own.tokens) / total,
        hidden,
      };
    })
    .sort((a, b) => a.provider.localeCompare(b.provider));
}

function chartLayers(cut: Cut): ChartLayer[] {
  const {shown, filters, names, palette, buckets} = cut;
  const layers: ChartLayer[] = [];
  for (const provider of [...new Set(shown.map(each => each.provider))].sort()) {
    const own = shown.filter(each => each.provider === provider);
    const unpriced = sum(
      volumeOf(
        own.filter(each => each.source === 'unpriced'),
        buckets,
      ),
    );
    // A provider no price covers has no cost to draw: a flat line at zero would say it was free.
    if (filters.tab === 'cost' && uncovered(sum(volumeOf(own, buckets)), unpriced)) continue;
    layers.push({
      provider,
      name: providerName(names, provider),
      series: palette.get(provider) ?? 0,
      values: filters.tab === 'cost' ? column(own, 'estimateUsd', buckets) : volumeOf(own, buckets),
    });
  }
  return layers;
}

const part = (key: string, label: string, value: number, figure: string): Part => ({
  key,
  label,
  value,
  figure,
});

const costPart = (key: string, label: string, value: number): Part =>
  part(key, label, value, dollars(value));

const tokenPart = (key: string, label: string, value: number): Part =>
  part(key, label, value, tokens(value));

/** The token totals of the shown rows. */
function tokenTotals(cut: Cut) {
  const {shown, buckets: n} = cut;
  const cached = sum(column(shown, 'cached', n));
  const uncached = sum(column(shown, 'uncached', n));
  const written = sum(column(shown, 'cacheWrite', n));
  return {
    cached,
    uncached,
    written,
    output: sum(column(shown, 'output', n)),
    input: cached + uncached + written,
  };
}

function tileRows(cut: Cut, totalTokens: number): Tile[] {
  const {shown, buckets: n} = cut;
  const {cached, uncached, output, input} = tokenTotals(cut);
  const counts: Tile[] = [
    {
      name: 'Processed',
      figure: tokens(totalTokens),
      detail: `in ${tokens(input)} · out ${tokens(output)}`,
    },
    {
      name: 'Cached',
      figure: tokens(cached),
      detail: input === 0 ? null : `${percent(cached / input)} of input`,
    },
    {name: 'Uncached', figure: tokens(uncached), detail: null},
    {name: 'Output', figure: tokens(output), detail: null},
  ];
  // Tokens are not dollars: a screen of tokens carries no tile of money.
  if (cut.filters.tab === 'tokens') return counts;
  return [
    ...counts,
    {
      name: 'Cache savings',
      figure: dollars(sum(column(shown, 'cacheSavingsUsd', n))),
      detail: `recorded ${dollars(sum(column(shown, 'recordedUsd', n)))}`,
    },
  ];
}

function costKindParts(cut: Cut): Part[] {
  const {shown, buckets: n} = cut;
  return [
    costPart('input', 'Input', sum(column(shown, 'inputUsd', n))),
    costPart('cache-read', 'Cache read', sum(column(shown, 'cacheReadUsd', n))),
    costPart('cache-write', 'Cache write', sum(column(shown, 'cacheWriteUsd', n))),
    costPart('output', 'Output', sum(column(shown, 'outputUsd', n))),
  ];
}

function speedParts(cut: Cut, totalCost: number): Part[] {
  const premium = sum(column(cut.shown, 'speedPremiumUsd', cut.buckets));
  return [
    costPart('standard', 'Standard', Math.max(0, totalCost - premium)),
    costPart('premium', 'Fast premium', premium),
  ];
}

function tokenKindParts(cut: Cut): Part[] {
  const {cached, uncached, written, output} = tokenTotals(cut);
  return [
    tokenPart('uncached', 'Uncached', uncached),
    tokenPart('cached', 'Cached', cached),
    tokenPart('cache-write', 'Cache write', written),
    tokenPart('output', 'Output', output),
  ];
}

/** The series of one provider and model, as a row: over every speed and source. */
function modelRow(
  cut: Cut,
  key: string,
  own: readonly DenseSeries[],
  overridden: ReadonlySet<string>,
) {
  const {filters, names, buckets: n} = cut;
  const provider = own[0]?.provider ?? '';
  const id = own[0]?.model ?? '';
  const cost = sum(column(own, 'estimateUsd', n));
  const count = sum(volumeOf(own, n));
  const unpriced = sum(
    volumeOf(
      own.filter(each => each.source === 'unpriced'),
      n,
    ),
  );
  const cached = sum(column(own, 'cached', n));
  const input = cached + sum(column(own, 'uncached', n)) + sum(column(own, 'cacheWrite', n));
  return {
    key,
    provider,
    providerName: providerName(names, provider),
    id,
    name: modelName(names, provider, id),
    costUsd: cost,
    cost: uncovered(count, unpriced) ? UNPRICED : dollars(cost),
    tokens: count,
    tokensFigure: tokens(count),
    turns: sum(column(own, 'turns', n)),
    perMillion:
      count === 0 || cost === 0 || uncovered(count, unpriced)
        ? null
        : dollars((cost / count) * 1_000_000),
    cacheHit: input === 0 ? null : cached / input,
    unpricedTokens: unpriced,
    share: 0,
    trend: filters.tab === 'cost' ? column(own, 'estimateUsd', n) : volumeOf(own, n),
    overridden: overridden.has(id),
  };
}

function modelRows(cut: Cut, overridden: ReadonlySet<string>): ModelRow[] {
  const {filters, shown} = cut;
  const grouped = new Map<string, DenseSeries[]>();
  for (const each of shown) {
    const key = `${each.provider}\u0000${each.model}`;
    grouped.set(key, [...(grouped.get(key) ?? []), each]);
  }
  const models = [...grouped.entries()]
    .map(([key, own]) => modelRow(cut, key, own, overridden))
    .sort(
      (a, b) =>
        byTab(filters.tab, b.costUsd, b.tokens) - byTab(filters.tab, a.costUsd, a.tokens) ||
        a.key.localeCompare(b.key),
    );
  const whole = sum(models.map(each => byTab(filters.tab, each.costUsd, each.tokens)));
  return models.map(each => ({
    ...each,
    share: whole === 0 ? 0 : byTab(filters.tab, each.costUsd, each.tokens) / whole,
  }));
}

function timeRows(cut: Cut, labels: readonly string[]): TimeRow[] {
  const {merged, shown, buckets: n} = cut;
  const estimate = column(shown, 'estimateUsd', n);
  const volume = volumeOf(shown, n);
  const unpriced = volumeOf(
    shown.filter(each => each.source === 'unpriced'),
    n,
  );
  const turns = column(shown, 'turns', n);
  return labels.map((label, at) => ({
    key: `${at}`,
    label,
    cost: uncovered(volume[at] ?? 0, unpriced[at] ?? 0) ? '—' : dollars(estimate[at] ?? 0),
    tokens: tokens(volume[at] ?? 0),
    turns: turns[at] ?? 0,
    runs: merged.runs[at] ?? 0,
    sessions: merged.sessions[at] ?? 0,
  }));
}

/** What the visible providers of `costs` spent. */
const visibleCost = (hidden: ReadonlySet<string>, costs: readonly ProviderCost[]): number =>
  costs
    .filter(each => !hidden.has(each.provider))
    .reduce((total, each) => total + each.estimateUsd, 0);

function runRows(cut: Cut): RunRow[] {
  const {merged, filters} = cut;
  if (filters.source === 'outside') return [];
  const costed = merged.runRows
    .map(row => ({row, cost: visibleCost(filters.hidden, row.costs)}))
    .filter(each => each.cost > 0);
  const total = sum(costed.map(each => each.cost));
  return costed.map(({row, cost}) => ({
    key: `${row.host}|${row.project}|${row.runId}`,
    host: row.host,
    project: row.project,
    runId: row.runId,
    costUsd: cost,
    cost: dollars(cost),
    share: total === 0 ? 0 : cost / total,
  }));
}

function agentRows(cut: Cut): AgentRow[] {
  const {merged, filters, names} = cut;
  if (filters.source === 'outside') return [];
  const costed = merged.agentRows
    .map(row => ({row, cost: visibleCost(filters.hidden, row.costs)}))
    .filter(each => each.cost > 0);
  const total = sum(costed.map(each => each.cost));
  return costed.map(({row, cost}) => ({
    key: `${row.role}|${row.model}`,
    role: roleName(row.role),
    model: row.model,
    modelName: names.find(each => each.model === row.model)?.modelName ?? plainName(row.model),
    cost: dollars(cost),
    share: total === 0 ? 0 : cost / total,
  }));
}

/** What the figure leaves out or only estimates, as chips. */
function chipRows(
  cut: Cut,
  totalTokens: number,
  unpricedModels: readonly string[],
  options: {
    readonly pricing: readonly UsagePricingEntry[];
    readonly unreadablePrices: boolean;
    readonly olderRunsUnread: boolean;
  },
): ChipView[] {
  const {shown, merged, buckets: n} = cut;
  const unpriced = sum(
    volumeOf(
      shown.filter(each => each.source === 'unpriced'),
      n,
    ),
  );
  const approximate = sum(column(shown, 'approximateTurns', n));
  const chips: ChipView[] = [];
  if (totalTokens > 0 && unpriced > 0) {
    chips.push({
      key: 'unpriced',
      label: `${percent(unpriced / totalTokens)} unpriced`,
      hint: unpricedModels.map(key => key.split('\u0000').join(' ')).join('\n') || null,
      tone: 'warn',
    });
  }
  if (approximate > 0) {
    chips.push({
      key: 'approximate',
      label: `${turnsWords(approximate)} approximate`,
      hint: null,
      tone: 'warn',
    });
  }
  const stale = options.pricing.find(each => each.status !== 'fresh');
  if (stale !== undefined) {
    chips.push({
      key: 'rates',
      label:
        stale.checkedOn === ''
          ? `${stale.status === 'bundled' ? 'Bundled' : 'Cached'} prices`
          : `Prices of ${stale.checkedOn}`,
      hint:
        stale.status === 'bundled'
          ? 'The prices that came with this version, not a fresh list'
          : 'The last price list fetched, not a fresh one',
      tone: 'neutral',
    });
  }
  if (options.unreadablePrices) {
    chips.push({key: 'prices', label: 'Prices unreadable', hint: null, tone: 'warn'});
  }
  if (options.olderRunsUnread) {
    chips.push({
      key: 'older',
      label: 'Older runs unread',
      hint: 'More projects than one question can name: only the runs a host has indexed are read',
      tone: 'neutral',
    });
  }
  if (merged.foldedSeries > 0) {
    chips.push({
      key: 'folded',
      label: `${merged.foldedSeries} folded`,
      hint: null,
      tone: 'neutral',
    });
  }
  return chips;
}

/** The sessions the figure is over: runs, a person's own sessions, or both. */
function sessionCount(merged: Merged, source: UsageSourceChoice): number {
  if (source === 'run') return merged.distinct.runs;
  if (source === 'outside') return merged.distinct.sessions;
  return merged.distinct.runs + merged.distinct.sessions;
}

/** The view of `merged` under `filters`; `names` and `pricing` word what the hosts only name. */
export function usageView(
  merged: Merged,
  filters: ViewFilters,
  options: {
    readonly names: readonly ModelName[];
    readonly edges: PeriodEdges;
    readonly pricing: readonly UsagePricingEntry[];
    readonly unreadablePrices: boolean;
    readonly olderRunsUnread: boolean;
    readonly overridden: ReadonlySet<string>;
  },
): UsageView {
  const {names, edges} = options;
  const buckets = merged.buckets;
  const palette = new Map(providersOf(merged).map((provider, at) => [provider, at % PALETTE]));
  const shown = merged.series.filter(
    each =>
      (filters.source === 'all' || each.origin === filters.source) &&
      !filters.hidden.has(each.provider),
  );
  const cut: Cut = {merged, filters, names, buckets, shown, palette};
  const totalCost = sum(column(shown, 'estimateUsd', buckets));
  const totalTokens = sum(volumeOf(shown, buckets));
  const models = modelRows(cut, options.overridden);
  const newBests = filters.source === 'outside' ? 0 : sum(merged.bests);
  const runSpend = sum(
    shown.filter(each => each.origin === 'run').map(each => sum(each.values.estimateUsd)),
  );
  const outsideCost = sum(
    shown.filter(each => each.origin === 'outside').map(each => sum(each.values.estimateUsd)),
  );
  const otherRuns =
    filters.source === 'outside' ? 0 : visibleCost(filters.hidden, merged.runsOther);
  const sessions = sessionCount(merged, filters.source);
  return {
    empty: shown.length === 0 || (totalCost === 0 && totalTokens === 0),
    tab: filters.tab,
    hero: {
      figure: filters.tab === 'cost' ? dollars(totalCost) : tokens(totalTokens),
      caption: `${sessions} ${sessions === 1 ? 'session' : 'sessions'} · ${filters.tab === 'cost' ? 'API estimate' : 'Processed'}`,
    },
    chips: chipRows(
      cut,
      totalTokens,
      models.filter(each => each.unpricedTokens > 0).map(each => each.key),
      options,
    ),
    providers: providerRows(cut, byTab(filters.tab, totalCost, totalTokens)),
    chart: {
      unit: filters.tab === 'cost' ? 'US dollars' : 'tokens',
      labels: Array.from({length: buckets}, (_, at) => bucketLabel(edges, at)),
      layers: chartLayers(cut),
    },
    tiles: tileRows(cut, totalTokens),
    costByKind: costKindParts(cut),
    bySpeed: speedParts(cut, totalCost),
    tokensByKind: tokenKindParts(cut),
    models,
    times: timeRows(
      cut,
      Array.from({length: buckets}, (_, at) => bucketLabel(edges, at)),
    ),
    runs: runRows(cut),
    runsOther: otherRuns > 0 ? dollars(otherRuns) : null,
    outside: filters.source !== 'run' && outsideCost > 0 ? dollars(outsideCost) : null,
    agents: agentRows(cut),
    perNewBest: newBests === 0 ? null : dollars(runSpend / newBests),
    newBests,
    perHourNow: dollars(visibleCost(filters.hidden, merged.recent)),
    unpricedModels: models.filter(each => each.unpricedTokens > 0).map(each => each.key),
  };
}

/** What a host is called in the host filter. */
export function hostName(host: string): string {
  return hostLabel(hostFromKey(host));
}
