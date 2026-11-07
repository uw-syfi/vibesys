/**
 * What a run has cost, as the host works it out (`proto/vibesys/orchestrator/v1/cost.proto`). The
 * host owns every number: the total, the split by type of agent and by model, the buckets of spend
 * over time and the rate of the last hour. A screen shows these and computes none of them, so this
 * file only changes their shape (bigints and timestamps become numbers) and keeps the latest view
 * of each run. It adds nothing up and orders nothing: every list is in the order the host sent it.
 *
 * Each view replaces the one before it, and the stream the views arrive on is retired before its
 * successor delivers, so the last one to arrive is the newest.
 */
import {
  type AgentRole,
  Harness,
  type ModelCost,
  type ModelRef,
  type RoleCost,
  type RunCost,
  type SpendBucket,
  type Usage,
} from '@vibesys/orchestrator-proto';
import {type Instant, ms} from './time.js';

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
}

export interface ModelCostView {
  readonly model: CostModel;
  readonly turns: number;
  readonly unpricedTurns: number;
  readonly usage: CostUsage;
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
}

const count = (n: bigint | undefined): number | null => (n === undefined ? null : Number(n));

function usageView(usage: Usage | undefined): CostUsage {
  return {
    inputTokens: count(usage?.inputTokens),
    cacheReadTokens: count(usage?.cacheReadTokens),
    cacheCreationTokens: count(usage?.cacheCreationTokens),
    outputTokens: count(usage?.outputTokens),
    reasoningTokens: count(usage?.reasoningTokens),
    costUsd: usage?.costUsd ?? null,
  };
}

const modelView = (model: ModelRef | undefined): CostModel => ({
  harness: model?.harness ?? Harness.UNSPECIFIED,
  model: model?.model ?? '',
});

const roleView = (role: RoleCost): RoleCostView => ({
  role: role.role,
  agents: role.agents,
  turns: role.turns,
  unpricedTurns: role.unpricedTurns,
  usage: usageView(role.usage),
});

const modelCostView = (cost: ModelCost): ModelCostView => ({
  model: modelView(cost.model),
  turns: cost.turns,
  unpricedTurns: cost.unpricedTurns,
  usage: usageView(cost.usage),
});

const bucketView = (bucket: SpendBucket): SpendBucketView => ({
  startMs: ms(bucket.start) ?? 0,
  costUsd: bucket.costUsd,
  byModel: bucket.byModel.map(spend => ({model: modelView(spend.model), costUsd: spend.costUsd})),
});

/** An instant, or a duration of the contract, which has the same two fields, in milliseconds. */
const instantMs = (at: Instant | undefined): number => ms(at) ?? 0;

/** The view a screen reads, from the message the host sent. */
export function costViewOf(cost: RunCost): CostView {
  return {
    runId: cost.runId,
    sequence: cost.sequence,
    asOfMs: instantMs(cost.asOf),
    total: usageView(cost.total),
    maxCostUsd: cost.maxCostUsd ?? null,
    turns: cost.turns,
    unpricedTurns: cost.unpricedTurns,
    roles: cost.roles.map(roleView),
    models: cost.models.map(modelCostView),
    series: {
      startMs: instantMs(cost.series?.start),
      bucketMs: instantMs(cost.series?.width),
      buckets: (cost.series?.buckets ?? []).map(bucketView),
    },
    lastHourUsd: cost.lastHourUsd ?? null,
  };
}
