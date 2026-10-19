/**
 * What usage records add up to, for any records: a run's, or the records of many runs (the run is
 * one more dimension, `run`). A pure reducer over `UsageRecord[]`, so a view of one run and a later
 * view across runs fold the same way.
 *
 * - `foldUsage` groups records by up to two dimensions and totals each group and the whole;
 * - `usageOverTime` cuts the stretch the records ended in into equal buckets and totals each
 *   bucket by one dimension.
 *
 * Every record lands in exactly one group and one bucket (a record with no end time in the
 * `undated` bucket), so group totals sum to the overall total and bucketing loses no record. Sums
 * follow the `TokenTally` rule: `null` until some record reports the count.
 */
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {addTokens, NO_TOKENS, type TokenTally} from '../token-usage.js';

/** The ways records group: a closed set. */
export const USAGE_DIMENSIONS = [
  'role',
  'harness',
  'model',
  'reasoning_effort',
  'round',
  'run',
  'idea',
] as const;
export type UsageDimension = (typeof USAGE_DIMENSIONS)[number];

/** What a set of records spent. */
export interface UsageTotals {
  readonly tokens: TokenTally;
  /** The cost the providers reported; `null` until some record reports one. */
  readonly costUsd: number | null;
  readonly turns: number;
}

export const NO_USAGE: UsageTotals = {tokens: NO_TOKENS, costUsd: null, turns: 0};

/** One group: the values of its dimensions (`null`: the journal does not hold the value). */
export interface UsageGroup {
  readonly key: readonly (string | null)[];
  readonly totals: UsageTotals;
}

export interface UsageFold {
  /** Groups in the order their first record came. */
  readonly groups: readonly UsageGroup[];
  readonly total: UsageTotals;
}

/**
 * The idea a record's turn served, or null (no idea: the turn is the run's own work). A record does
 * not name its idea: the run's trace does, so the caller joins them and hands the join in.
 */
export type IdeaOf = (record: UsageRecord) => string | null;

const NO_IDEA: IdeaOf = () => null;

/** The value of `record` on `dimension`. */
export function dimensionValue(
  record: UsageRecord,
  dimension: UsageDimension,
  ideaOf: IdeaOf = NO_IDEA,
): string | null {
  switch (dimension) {
    case 'role':
      return record.role;
    case 'harness':
      return record.harness;
    case 'model':
      return record.model;
    case 'reasoning_effort':
      return record.reasoning_effort;
    case 'round':
      return record.round_label;
    case 'run':
      return record.run_id;
    case 'idea':
      return ideaOf(record);
  }
}

function tallyOf(record: UsageRecord): TokenTally {
  const {tokens} = record;
  return {
    input: tokens.input,
    cacheRead: tokens.cache_read,
    cacheWrite: tokens.cache_write,
    output: tokens.output,
    reasoning: tokens.reasoning,
  };
}

function plus(a: number | null, b: number | null): number | null {
  if (a === null) return b;
  return b === null ? a : a + b;
}

/** `totals` with one more record. */
export function addRecord(totals: UsageTotals, record: UsageRecord): UsageTotals {
  return {
    tokens: addTokens(totals.tokens, tallyOf(record)),
    costUsd: plus(totals.costUsd, record.cost_usd),
    turns: totals.turns + 1,
  };
}

/** What `records` spent in all. */
export function totalUsage(records: readonly UsageRecord[]): UsageTotals {
  return records.reduce(addRecord, NO_USAGE);
}

function keyOf(
  record: UsageRecord,
  by: readonly UsageDimension[],
  ideaOf: IdeaOf,
): readonly (string | null)[] {
  return by.map(dimension => dimensionValue(record, dimension, ideaOf));
}

/** `records` grouped by `by` (any number of dimensions; none is one group of everything). */
export function foldUsage(
  records: readonly UsageRecord[],
  by: readonly UsageDimension[],
  ideaOf: IdeaOf = NO_IDEA,
): UsageFold {
  const groups = new Map<string, {key: readonly (string | null)[]; totals: UsageTotals}>();
  for (const record of records) {
    const key = keyOf(record, by, ideaOf);
    const id = JSON.stringify(key);
    const group = groups.get(id) ?? {key, totals: NO_USAGE};
    groups.set(id, {key, totals: addRecord(group.totals, record)});
  }
  return {groups: [...groups.values()], total: totalUsage(records)};
}

/** One slice of time: `[from, to)` in epoch milliseconds, and what each group spent in it. */
export interface UsageBucket {
  readonly from: number;
  readonly to: number;
  readonly groups: readonly UsageGroup[];
  readonly total: UsageTotals;
}

export interface UsageOverTime {
  /** The buckets, oldest first; none when no record has an end time. */
  readonly buckets: readonly UsageBucket[];
  /** Records with no end time: they have no place on the time axis. */
  readonly undated: UsageTotals;
  /** The distinct group keys over every bucket, in the order they first came. */
  readonly keys: readonly (string | null)[];
}

/**
 * The records' end times cut into `count` equal buckets from the earliest to the latest (the
 * latest is in the last bucket), each bucket's records grouped by `by`.
 */
export function usageOverTime(
  records: readonly UsageRecord[],
  by: UsageDimension,
  count: number,
  ideaOf: IdeaOf = NO_IDEA,
): UsageOverTime {
  const dated: {record: UsageRecord; at: number}[] = [];
  let undated = NO_USAGE;
  for (const record of records) {
    const at = record.ended_at === null ? Number.NaN : Date.parse(record.ended_at);
    if (Number.isFinite(at)) dated.push({record, at});
    else undated = addRecord(undated, record);
  }
  const keys = [...new Set(records.map(record => dimensionValue(record, by, ideaOf)))];
  if (dated.length === 0) return {buckets: [], undated, keys};
  const first = Math.min(...dated.map(item => item.at));
  const last = Math.max(...dated.map(item => item.at));
  const size = Math.max(1, Math.floor(count));
  const width = (last - first) / size;
  const inBucket = (at: number): number =>
    width === 0 ? 0 : Math.min(size - 1, Math.floor((at - first) / width));
  const buckets = Array.from({length: width === 0 ? 1 : size}, (_, index) => {
    const members = dated.filter(item => inBucket(item.at) === index).map(item => item.record);
    const fold = foldUsage(members, [by], ideaOf);
    return {
      from: first + index * width,
      to: width === 0 ? last : first + (index + 1) * width,
      groups: fold.groups,
      total: fold.total,
    };
  });
  return {buckets, undated, keys};
}
