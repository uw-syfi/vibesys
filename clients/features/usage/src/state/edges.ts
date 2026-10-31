/**
 * The periods of time the Usage page offers and the bucket edges each asks the hosts about.
 *
 * The page cuts the buckets, in the time zone the person reads their days in, and sends the cuts:
 * a host needs no time zone data and no clock, and every host answers in the same buckets. Hours
 * and days are the local hours and days of that zone, so a day is 23 or 25 hours long where the
 * clocks change. Every edge is a whole quarter hour, which every zone offset is a multiple of and
 * which is the grain a host rolls its runs up to.
 *
 * Pure over a time and a zone name: this module reads no clock and no ambient zone.
 */
import {USAGE_QUARTER_MS} from '@vibesys/hosts';

export type UsagePeriod = '24h' | '7d' | '30d' | '90d';

export const USAGE_PERIODS: readonly {readonly value: UsagePeriod; readonly label: string}[] = [
  {value: '24h', label: 'Past 24 hours'},
  {value: '7d', label: 'Past 7 days'},
  {value: '30d', label: 'Past 30 days'},
  {value: '90d', label: 'Past 90 days'},
];

/** How long a bucket is: an hour for the past day, else a local day. */
export type BucketGrain = 'hour' | 'day';

/** The buckets of a period: bucket `i` is `[edges[i], edges[i + 1])`, in milliseconds. */
export interface PeriodEdges {
  readonly period: UsagePeriod;
  readonly grain: BucketGrain;
  readonly zone: string;
  readonly edges: readonly number[];
}

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
const DAYS: Readonly<Record<Exclude<UsagePeriod, '24h'>, number>> = {'7d': 7, '30d': 30, '90d': 90};

interface Parts {
  readonly year: number;
  readonly month: number;
  readonly day: number;
  readonly hour: number;
  readonly minute: number;
  readonly second: number;
}

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(zone: string): Intl.DateTimeFormat {
  let found = formatters.get(zone);
  if (found === undefined) {
    found = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      hourCycle: 'h23',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      second: 'numeric',
    });
    formatters.set(zone, found);
  }
  return found;
}

function partsOf(at: number, zone: string): Parts {
  const found: Record<string, number> = {};
  for (const part of formatter(zone).formatToParts(at)) {
    if (part.type !== 'literal') found[part.type] = Number(part.value);
  }
  return {
    year: found['year'] ?? 0,
    month: found['month'] ?? 1,
    day: found['day'] ?? 1,
    hour: found['hour'] ?? 0,
    minute: found['minute'] ?? 0,
    second: found['second'] ?? 0,
  };
}

/** The offset of `zone` from UTC at `at`, in milliseconds: local wall time minus the instant. */
export function zoneOffsetMs(at: number, zone: string): number {
  const p = partsOf(at, zone);
  const local = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return local - Math.floor(at / 1000) * 1000;
}

/** The local calendar day of `at` as a number that increases by one a day. */
function dayNumber(at: number, zone: string): number {
  const p = partsOf(at, zone);
  return Math.floor(Date.UTC(p.year, p.month - 1, p.day) / DAY_MS);
}

/**
 * The first instant of the local day that holds `at`. A zone that skips midnight (a clock change
 * at 00:00) starts its day at the first instant that exists.
 */
export function startOfLocalDay(at: number, zone: string): number {
  const target = dayNumber(at, zone);
  const p = partsOf(at, zone);
  let start = at - (((p.hour * 60 + p.minute) * 60 + p.second) * 1000 + (at % 1000));
  // Clock changes inside the day make the first guess land on another day or late: walk by
  // quarter hours (every offset and every change is a multiple of one) to the first instant.
  while (dayNumber(start - USAGE_QUARTER_MS, zone) === target) start -= USAGE_QUARTER_MS;
  while (dayNumber(start, zone) !== target) start += USAGE_QUARTER_MS;
  return start;
}

/** The first instant of the local hour that holds `at`. */
function startOfLocalHour(at: number, zone: string): number {
  const offset = zoneOffsetMs(at, zone);
  const local = at + offset;
  return Math.floor(local / HOUR_MS) * HOUR_MS - offset;
}

/** The edges of `period` ending with the bucket that holds `now`, in `zone`. */
export function periodEdges(period: UsagePeriod, now: number, zone: string): PeriodEdges {
  if (period === '24h') {
    const last = startOfLocalHour(now, zone) + HOUR_MS;
    return {
      period,
      grain: 'hour',
      zone,
      edges: Array.from({length: 25}, (_, at) => last - (24 - at) * HOUR_MS),
    };
  }
  const count = DAYS[period];
  const today = startOfLocalDay(now, zone);
  const edges = [startOfLocalDay(today + 36 * HOUR_MS, zone)];
  let day = today;
  for (let at = 0; at < count; at += 1) {
    edges.unshift(day);
    day = startOfLocalDay(day - 1, zone);
  }
  return {period, grain: 'day', zone, edges};
}

/** What bucket `index` of `shown` is called: `13:00` for an hour, `Oct 31` for a day. */
export function bucketLabel(shown: PeriodEdges, index: number): string {
  const at = shown.edges[index];
  if (at === undefined) return '';
  return new Intl.DateTimeFormat(
    'en-US',
    shown.grain === 'hour'
      ? {timeZone: shown.zone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}
      : {timeZone: shown.zone, month: 'short', day: 'numeric'},
  ).format(at);
}

/** The bucket of `shown` that holds `at`, or null outside it. */
export function bucketOf(shown: PeriodEdges, at: number): number | null {
  const {edges} = shown;
  const first = edges[0];
  const last = edges[edges.length - 1];
  if (first === undefined || last === undefined || at < first || at >= last) return null;
  let low = 0;
  let high = edges.length - 1;
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if ((edges[middle] ?? Number.POSITIVE_INFINITY) <= at) low = middle;
    else high = middle;
  }
  return low;
}
