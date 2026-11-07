/**
 * What a run has cost, as the host works it out (`proto/vibesys/orchestrator/v1/cost.proto`). The
 * host owns every number: the total, the split by type of agent and by model, the buckets of spend
 * over time and the rate of the last hour. A screen shows these and computes none of them, so this
 * file only changes their shape (bigints and timestamps become numbers) and keeps the latest view
 * of each run. It adds nothing up and orders nothing: every list is in the order the host sent it.
 *
 * Each view replaces the one before it, unless it is older than the one held (`applyCost`).
 *
 * The contract requires some fields (the instant, the total, the series with its start and width,
 * the model of each entry). A message that lacks one is not a view with a default in its place: the
 * read names the field (`readCost`), and the feed fails with it, shown beside the screen's status.
 */
import type {
  AgentRole,
  Harness,
  ModelCost,
  ModelRef,
  RoleCost,
  RunCost,
  SpendBucket,
  Usage,
} from '@vibesys/orchestrator-proto';
import type {FeedStatus} from '../feed.js';
import type {Freshness} from '../freshness.js';
import {type Instant, ms} from './time.js';

/** How a run's cost stream stands, for the screen that reads what it delivers. */
export interface CostFeedSnapshot {
  readonly status: FeedStatus;
  readonly freshness: Freshness;
}

/** Tokens by kind and the cost of the priced turns; a field no turn reported is null, never zero. */
export interface CostUsage {
  readonly inputTokens: number | null;
  readonly cacheReadTokens: number | null;
  readonly cacheCreationTokens: number | null;
  readonly outputTokens: number | null;
  readonly reasoningTokens: number | null;
  readonly costUsd: number | null;
}

/** The model an agent ran on; an empty `model` is the harness's own default. */
export interface CostModel {
  readonly harness: Harness;
  readonly model: string;
}

export interface RoleCostView {
  readonly role: AgentRole;
  /** How many agents of the type the run has had, over or not. */
  readonly agents: number;
  readonly turns: number;
  /** Turns that reported tokens and no cost: the type's cost leaves them out. */
  readonly unpricedTurns: number;
  readonly usage: CostUsage;
  /** All the type's tokens, as the host added them up. */
  readonly totalTokens: number;
}

export interface ModelCostView {
  readonly model: CostModel;
  readonly turns: number;
  readonly unpricedTurns: number;
  readonly usage: CostUsage;
  /** All the model's tokens, as the host added them up. */
  readonly totalTokens: number;
}

export interface SpendBucketView {
  readonly startMs: number;
  readonly costUsd: number;
  /** In the order of `CostView.models`, leaving out a model with no cost in the bucket. */
  readonly byModel: readonly {readonly model: CostModel; readonly costUsd: number}[];
}

export interface CostView {
  readonly runId: string;
  /** The log sequence the view is current to. */
  readonly sequence: bigint;
  /** The instant the view is worked out at: the host's clock while the run lives, its end after. */
  readonly asOfMs: number;
  readonly total: CostUsage;
  /** The run's cost limit, or null when it has none. */
  readonly maxCostUsd: number | null;
  readonly turns: number;
  /** How many finished turns have tokens and no cost; any is a reason to call the cost a floor. */
  readonly unpricedTurns: number;
  /** Most costly first; a type with no priced turn comes last. */
  readonly roles: readonly RoleCostView[];
  /** Most costly first; a model with no priced turn comes last. */
  readonly models: readonly ModelCostView[];
  readonly series: {
    readonly startMs: number;
    readonly bucketMs: number;
    readonly buckets: readonly SpendBucketView[];
  };
  /** What the turns that finished in the hour before `asOfMs` cost; null when none is priced. */
  readonly lastHourUsd: number | null;
  /** The host left a model out of the lists of models, or cut a model's name; every other figure is whole. */
  readonly modelsTruncated: boolean;
}

const count = (n: bigint | undefined): number | null => (n === undefined ? null : Number(n));

/** The read's own signal that the message lacks a field the contract requires. */
class MissingField extends Error {
  constructor(readonly field: string) {
    super(`${field} is absent`);
  }
}

/** The required field, or the read stops naming it. */
function need<T>(value: T | undefined, field: string): T {
  if (value === undefined) throw new MissingField(field);
  return value;
}

/** An instant, or a duration of the contract, which has the same two fields, in milliseconds. */
const instantMs = (at: Instant | undefined, field: string): number => {
  const known = ms(need(at, field));
  if (known === null) throw new MissingField(field);
  return known;
};

function usageView(usage: Usage | undefined, field: string): CostUsage {
  const held = need(usage, field);
  return {
    inputTokens: count(held.inputTokens),
    cacheReadTokens: count(held.cacheReadTokens),
    cacheCreationTokens: count(held.cacheCreationTokens),
    outputTokens: count(held.outputTokens),
    reasoningTokens: count(held.reasoningTokens),
    costUsd: held.costUsd ?? null,
  };
}

function modelView(model: ModelRef | undefined, field: string): CostModel {
  const named = need(model, field);
  return {harness: named.harness, model: named.model};
}

const roleView = (role: RoleCost): RoleCostView => ({
  role: role.role,
  agents: role.agents,
  turns: role.turns,
  unpricedTurns: role.unpricedTurns,
  usage: usageView(role.usage, 'roles.usage'),
  totalTokens: Number(role.totalTokens),
});

const modelCostView = (cost: ModelCost): ModelCostView => ({
  model: modelView(cost.model, 'models.model'),
  turns: cost.turns,
  unpricedTurns: cost.unpricedTurns,
  usage: usageView(cost.usage, 'models.usage'),
  totalTokens: Number(cost.totalTokens),
});

const bucketView = (bucket: SpendBucket): SpendBucketView => ({
  startMs: instantMs(bucket.start, 'series.buckets.start'),
  costUsd: bucket.costUsd,
  byModel: bucket.byModel.map(spend => ({
    model: modelView(spend.model, 'series.buckets.by_model.model'),
    costUsd: spend.costUsd,
  })),
});

/** What reading a message came to: the view a screen shows, or the required field it lacks. */
export type CostRead =
  | {readonly _tag: 'View'; readonly view: CostView}
  | {readonly _tag: 'Missing'; readonly field: string};

/**
 * The view a screen reads, from the message the host sent. The contract's validation rules
 * require those fields, so a message that lacks one comes from a host that broke the contract;
 * the read names the field and shows no zero or empty name in its place.
 */
export function readCost(cost: RunCost): CostRead {
  try {
    const series = need(cost.series, 'series');
    return {
      _tag: 'View',
      view: {
        runId: cost.runId,
        sequence: cost.sequence,
        asOfMs: instantMs(cost.asOf, 'as_of'),
        total: usageView(cost.total, 'total'),
        maxCostUsd: cost.maxCostUsd ?? null,
        turns: cost.turns,
        unpricedTurns: cost.unpricedTurns,
        roles: cost.roles.map(roleView),
        models: cost.models.map(modelCostView),
        series: {
          startMs: instantMs(series.start, 'series.start'),
          bucketMs: instantMs(series.width, 'series.width'),
          buckets: series.buckets.map(bucketView),
        },
        lastHourUsd: cost.lastHourUsd ?? null,
        modelsTruncated: cost.modelsTruncated,
      },
    };
  } catch (error) {
    // Only the read's own signal is caught: any other failure is a defect and propagates.
    if (error instanceof MissingField) return {_tag: 'Missing', field: error.field};
    throw error;
  }
}
