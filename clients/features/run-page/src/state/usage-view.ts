/**
 * The Usage face: what the run's records (`UsageRecord`, one per agent turn) add up to, in words
 * and shapes the face draws. Its controls (`UsageControls`) are the page's state: which count the
 * chart shows, and by which dimensions the table groups. Every figure is a fold of the same
 * records (`@vibesys/run-state/usage`), so the totals, the table and the chart cannot disagree,
 * and the totals are the run's own (the header's spend is the same turns' sum).
 */
import {roleName} from '@vibesys/contracts';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {
  foldUsage,
  type UsageDimension,
  type UsageGroup,
  type UsageTotals,
  usageOverTime,
} from '@vibesys/run-state/usage';
import {roleSeries, SERIES_SLOTS} from './role-series.js';
import {compactTokens, type TokensReading, tokensReading} from './token-words.js';

/** The count the chart draws and the table sorts by. */
export type UsageMetric = 'input' | 'cached' | 'output' | 'cost';

export const USAGE_METRICS: readonly {readonly value: UsageMetric; readonly label: string}[] = [
  {value: 'input', label: 'Input'},
  {value: 'cached', label: 'Cached'},
  {value: 'output', label: 'Output'},
  {value: 'cost', label: 'Cost'},
];

/** The dimensions the table can group by, in the order a menu offers them. */
export const USAGE_GROUPS: readonly {readonly value: UsageDimension; readonly label: string}[] = [
  {value: 'role', label: 'Role'},
  {value: 'model', label: 'Model'},
  {value: 'harness', label: 'Harness'},
];

export interface UsageControls {
  readonly metric: UsageMetric;
  readonly by: UsageDimension;
  /** A second dimension within the first; null: none. */
  readonly within: UsageDimension | null;
}

export const USAGE_CONTROLS_DEFAULT: UsageControls = {metric: 'input', by: 'role', within: null};

/** `controls` with `by` changed; a second dimension equal to it is dropped. */
export function groupedBy(controls: UsageControls, by: UsageDimension): UsageControls {
  return {...controls, by, within: controls.within === by ? null : controls.within};
}

/** How many buckets the chart cuts the run's time into. */
const BUCKETS = 24;

const UNKNOWN = 'unknown';

function metricValue(totals: UsageTotals, metric: UsageMetric): number | null {
  switch (metric) {
    case 'input':
      return totals.tokens.input;
    case 'cached':
      return totals.tokens.cacheRead;
    case 'output':
      return totals.tokens.output;
    case 'cost':
      return totals.costUsd;
  }
}

function dollars(amount: number | null): string {
  return amount === null ? UNKNOWN : `$${amount.toFixed(2)}`;
}

function count(amount: number | null): string {
  return amount === null ? UNKNOWN : compactTokens(amount);
}

function figure(totals: UsageTotals, metric: UsageMetric): string {
  const value = metricValue(totals, metric);
  return metric === 'cost' ? dollars(value) : count(value);
}

/** One part of a group's name: words, or an identifier a person may copy (a model id). */
export interface UsageLabel {
  readonly text: string;
  readonly id: boolean;
}

/** A dimension's value as the face names it: a role in words, any other as the journal spells it. */
function labelOf(dimension: UsageDimension, value: string | null): UsageLabel {
  if (value === null) return {text: UNKNOWN, id: false};
  return dimension === 'role' ? {text: roleName(value), id: false} : {text: value, id: true};
}

/** A palette slot for a group: a role keeps its role color; others take slots in their order. */
function seriesOf(by: UsageDimension, key: string | null, index: number): number {
  return by === 'role' && key !== null ? roleSeries(key) : index % SERIES_SLOTS;
}

export interface UsageRowView {
  readonly id: string;
  readonly label: readonly UsageLabel[];
  readonly swatch: number;
  readonly input: string;
  readonly cached: string;
  readonly output: string;
  readonly cost: string;
  readonly turns: string;
  /** The row's share of the chosen metric's total, `42%`; empty when the total is unknown or zero. */
  readonly share: string;
}

export interface UsageBarView {
  readonly id: string;
  readonly segments: readonly {
    readonly key: string;
    readonly series: number;
    readonly value: number;
  }[];
  readonly total: number;
  /** What the bar stands for, in full, for the pointer and assistive technology. */
  readonly title: string;
}

export interface UsageView {
  /** The records, none: the face says so instead of drawing empty figures. */
  readonly empty: boolean;
  /** The run's spend as the header words it; null when no turn reported tokens. */
  readonly spent: TokensReading | null;
  readonly cost: string;
  readonly turns: string;
  readonly rows: readonly UsageRowView[];
  /** The chart's bars, oldest first, and the legend of the groups they stack. */
  readonly bars: readonly UsageBarView[];
  readonly legend: readonly {
    readonly label: string;
    readonly series: number;
    readonly id: boolean;
  }[];
  /** What the chart's height measures: `tokens` or `US dollars`. */
  readonly unit: string;
  /** The stretch of time the bars cover, in words; null when no record has an end time. */
  readonly span: {readonly from: string; readonly to: string} | null;
  /** Records with no end time, which the bars leave out; 0 when every record is placed. */
  readonly undated: number;
}

const WHEN = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

function byMetric(metric: UsageMetric) {
  return (a: UsageGroup, b: UsageGroup): number =>
    (metricValue(b.totals, metric) ?? -1) - (metricValue(a.totals, metric) ?? -1);
}

function rowOf(
  group: UsageGroup,
  by: readonly UsageDimension[],
  colors: ReadonlyMap<string | null, number>,
  total: number | null,
  controls: UsageControls,
): UsageRowView {
  const value = metricValue(group.totals, controls.metric);
  return {
    id: JSON.stringify(group.key),
    label: group.key.map((value, at) => labelOf(by[at] ?? controls.by, value)),
    swatch: colors.get(group.key[0] ?? null) ?? 0,
    input: count(group.totals.tokens.input),
    cached: count(group.totals.tokens.cacheRead),
    output: count(group.totals.tokens.output),
    cost: dollars(group.totals.costUsd),
    turns: String(group.totals.turns),
    share:
      value === null || total === null || total === 0
        ? ''
        : `${Math.round((value / total) * 100)}%`,
  };
}

/** The Usage face's view of `records` under `controls`. */
export function usageView(records: readonly UsageRecord[], controls: UsageControls): UsageView {
  const by = controls.within === null ? [controls.by] : [controls.by, controls.within];
  const fold = foldUsage(records, by);
  const total = metricValue(fold.total, controls.metric);
  const series = usageOverTime(records, controls.by, BUCKETS);
  const colors = new Map(
    series.keys.map((key, index) => [key, seriesOf(controls.by, key, index)] as const),
  );
  const first = series.buckets[0];
  const last = series.buckets.at(-1);
  const bars = series.buckets.map((bucket, index): UsageBarView => {
    const segments = bucket.groups.flatMap(group => {
      const value = metricValue(group.totals, controls.metric);
      const key = group.key[0] ?? null;
      return value === null || value === 0
        ? []
        : [{key: key ?? UNKNOWN, series: colors.get(key) ?? 0, value}];
    });
    return {
      id: `bar-${index}`,
      segments,
      total: segments.reduce((sum, segment) => sum + segment.value, 0),
      title: `${WHEN.format(bucket.from)}: ${figure(bucket.total, controls.metric)} over ${bucket.total.turns} turns`,
    };
  });
  return {
    empty: records.length === 0,
    spent: tokensReading(fold.total.tokens),
    cost: dollars(fold.total.costUsd),
    turns: String(fold.total.turns),
    rows: [...fold.groups]
      .sort(byMetric(controls.metric))
      .map(group => rowOf(group, by, colors, total, controls)),
    bars,
    legend: series.keys.map(key => {
      const {text, id} = labelOf(controls.by, key);
      return {label: text, series: colors.get(key) ?? 0, id};
    }),
    unit: controls.metric === 'cost' ? 'US dollars' : 'tokens',
    span:
      first === undefined || last === undefined
        ? null
        : {from: WHEN.format(first.from), to: WHEN.format(last.to)},
    undated: series.undated.turns,
  };
}
