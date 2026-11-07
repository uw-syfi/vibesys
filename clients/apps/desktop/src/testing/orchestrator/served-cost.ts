/**
 * A cost view as the message the host would send for it, so the fixture page and the tests that
 * serve a cost through the Fake daemon say it in the contract's own terms. Nothing here works
 * anything out: each field of the view is carried across as it is.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  ModelCostSchema,
  type ModelRef,
  ModelRefSchema,
  RoleCostSchema,
  type RunCost,
  RunCostSchema,
  SpendBucketSchema,
  type Usage,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import type {CostModel, CostUsage, CostView} from '../../state/index.js';

const tokens = (count: number | null): bigint | undefined =>
  count === null ? undefined : BigInt(count);

const usageOf = (usage: CostUsage): Usage =>
  create(UsageSchema, {
    inputTokens: tokens(usage.inputTokens),
    cacheReadTokens: tokens(usage.cacheReadTokens),
    cacheCreationTokens: tokens(usage.cacheCreationTokens),
    outputTokens: tokens(usage.outputTokens),
    reasoningTokens: tokens(usage.reasoningTokens),
    costUsd: usage.costUsd ?? undefined,
  });

const modelOf = (model: CostModel): ModelRef => create(ModelRefSchema, model);

/** The message for `view`, as the host would send it for the run `runId`. */
export function servedCost(view: CostView, runId: string): RunCost {
  const width = BigInt(Math.round(view.series.bucketMs / 1000));
  return create(RunCostSchema, {
    runId,
    sequence: view.sequence,
    asOf: timestampFromMs(view.asOfMs),
    total: usageOf(view.total),
    maxCostUsd: view.maxCostUsd ?? undefined,
    turns: view.turns,
    unpricedTurns: view.unpricedTurns,
    roles: view.roles.map(role =>
      create(RoleCostSchema, {
        role: role.role,
        agents: role.agents,
        turns: role.turns,
        unpricedTurns: role.unpricedTurns,
        usage: usageOf(role.usage),
        totalTokens: BigInt(role.totalTokens),
      }),
    ),
    models: view.models.map(cost =>
      create(ModelCostSchema, {
        model: modelOf(cost.model),
        turns: cost.turns,
        unpricedTurns: cost.unpricedTurns,
        usage: usageOf(cost.usage),
        totalTokens: BigInt(cost.totalTokens),
      }),
    ),
    series: {
      start: timestampFromMs(view.series.startMs),
      width: {seconds: width, nanos: 0},
      buckets: view.series.buckets.map(bucket =>
        create(SpendBucketSchema, {
          start: timestampFromMs(bucket.startMs),
          costUsd: bucket.costUsd,
          byModel: bucket.byModel.map(spend => ({
            model: modelOf(spend.model),
            costUsd: spend.costUsd,
          })),
        }),
      ),
    },
    lastHourUsd: view.lastHourUsd ?? undefined,
    modelsTruncated: view.modelsTruncated,
  });
}
