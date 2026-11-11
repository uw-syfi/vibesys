/**
 * How the Cost page chooses what to show of a host cost view it has already been served: which of
 * the served buckets of spend the chart draws (a range of the time axis) and in which order the
 * runs are listed (a sort of the host's list). Both are display choices over served data. Nothing
 * here adds a cost up, and no bucket is made or merged: the chart draws the host's own buckets (the
 * host picked their width for the whole span) and keeps or drops them whole. A range counts back
 * from the view's `asOfMs`, the instant the host worked the view out at, never from a clock.
 */
import type {SpendBucketView, SpendSeriesView} from './cost.js';
import type {HostRunCostView} from './host-cost.js';
import type {SortDirection} from './report/ideas.js';

const DAY_MS = 86_400_000;

/** How far back the chart looks: the last day, the last week, or everything the host served. */
export type SpendRange = '1d' | '7d' | 'all';

const RANGE_MS: Readonly<Record<Exclude<SpendRange, 'all'>, number>> = {
  '1d': DAY_MS,
  '7d': 7 * DAY_MS,
};

/** Whether a bucket holds spend: a priced turn ended in it. */
const hasSpend = (bucket: SpendBucketView): boolean =>
  bucket.costUsd > 0 || bucket.byModel.length > 0;

/** The places of the first and last bucket with spend; null when none has any. */
function spendEnds(buckets: readonly SpendBucketView[]): {first: number; last: number} | null {
  const first = buckets.findIndex(hasSpend);
  if (first < 0) return null;
  const last = buckets.length - 1 - [...buckets].reverse().findIndex(hasSpend);
  return {first, last};
}

/**
 * What the series covers: from the start of its first bucket with spend to the end of its last, in
 * milliseconds; 0 when no bucket has any. A series is served from the first run's start to `asOf`,
 * so a run that spent in its first minutes and then idled for days has a long series and a short span.
 */
export function spendSpanMs(series: SpendSeriesView): number {
  const ends = spendEnds(series.buckets);
  return ends === null ? 0 : (ends.last - ends.first + 1) * series.bucketMs;
}

/**
 * The ranges the chart offers: none while the spend spans a day or less (one axis says it all), then
 * the last day, the last week when the span is longer than a week, and always the whole span.
 */
export function rangesOf(series: SpendSeriesView): readonly SpendRange[] {
  const span = spendSpanMs(series);
  if (span <= DAY_MS) return [];
  return span > RANGE_MS['7d'] ? ['1d', '7d', 'all'] : ['1d', 'all'];
}

/** The range the chart shows for a choice: the choice while it is offered, else the whole span. */
export const rangeOf = (series: SpendSeriesView, choice: SpendRange): SpendRange =>
  rangesOf(series).includes(choice) ? choice : 'all';

/**
 * The buckets the chart draws for a range. `all` fits the data: from the first bucket with spend to
 * the last, so a series that begins long before its first turn, or goes on idle after its last, is
 * not drawn as an empty axis. `1d` and `7d` keep the buckets that end after that long before `asOfMs`,
 * idle ones too, since an axis of the last day says how long ago the last spend was.
 */
export function bucketsInRange(
  series: SpendSeriesView,
  asOfMs: number,
  range: SpendRange,
): readonly SpendBucketView[] {
  const {buckets, bucketMs} = series;
  if (range === 'all') {
    const ends = spendEnds(buckets);
    return ends === null ? [] : buckets.slice(ends.first, ends.last + 1);
  }
  const from = asOfMs - RANGE_MS[range];
  return buckets.filter(bucket => bucket.startMs + bucketMs > from);
}

/** The columns the list of runs can be ordered by. */
export type RunSortKey = 'run' | 'turns' | 'tokens' | 'cost' | 'budget';

export interface RunSort {
  readonly key: RunSortKey;
  readonly direction: SortDirection;
}

/** The order the host serves its runs in: most costly first. */
export const HOST_ORDER: RunSort = {key: 'cost', direction: 'descending'};

/** The order a column is first sorted in when it is chosen: names from A, figures largest first. */
const FIRST_DIRECTION: Readonly<Record<RunSortKey, SortDirection>> = {
  run: 'ascending',
  turns: 'descending',
  tokens: 'descending',
  cost: 'descending',
  budget: 'descending',
};

/** The sort a press of a header asks for: the same column turns over, another starts in its first order. */
export function nextRunSort(current: RunSort, pressed: RunSortKey): RunSort {
  if (current.key !== pressed) return {key: pressed, direction: FIRST_DIRECTION[pressed]};
  return {key: pressed, direction: current.direction === 'ascending' ? 'descending' : 'ascending'};
}

type Sortable = number | string | null;

/** The value a run is ordered by in each column; null where it has none, which always comes last. */
const SORT_VALUE: Readonly<Record<RunSortKey, (run: HostRunCostView) => Sortable>> = {
  run: run => run.name.toLowerCase(),
  turns: run => run.turns,
  tokens: run => run.totalTokens,
  cost: run => run.usage.costUsd,
  budget: run => run.maxCostUsd,
};

function compared(a: Sortable, b: Sortable, sign: 1 | -1): number {
  if (a === null || b === null) return a === b ? 0 : a === null ? 1 : -1;
  return (a < b ? -1 : a > b ? 1 : 0) * sign;
}

/**
 * The runs ordered by a column. A run with no value in the column (no price, no budget) comes last
 * whichever way it is ordered, and runs that tie keep the host's order, so a table never shuffles
 * as a live view is replaced.
 */
export function sortRuns(
  runs: readonly HostRunCostView[],
  sort: RunSort,
): readonly HostRunCostView[] {
  const sign = sort.direction === 'ascending' ? 1 : -1;
  return runs
    .map((run, at) => ({run, at, value: SORT_VALUE[sort.key](run)}))
    .sort((a, b) => compared(a.value, b.value, sign) || a.at - b.at)
    .map(each => each.run);
}
