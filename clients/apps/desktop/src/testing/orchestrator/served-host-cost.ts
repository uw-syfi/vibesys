/**
 * The cost of a host as the host serves it, for the Fake host: worked out once, in test code, from the
 * runs, agents and finished turns of a scenario, so the Cost page shows the numbers its Runs page
 * stands on. The app computes none of this (it reads `HostCost`); the Fake is the only place besides
 * the host that knows how it is made, so a fixture never states a cost of its own.
 *
 * - A turn is counted when it has finished. A turn with no cost is unpriced: it adds its tokens and
 *   itself to every figure and never a dollar.
 * - A turn belongs to its agent's run, role and model (the agent's harness and model).
 * - Lists are most costly first; what has no priced turn comes after, in the order of its name.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  type Agent,
  type HostCost,
  HostCostSchema,
  HostRunCostSchema,
  ModelCostSchema,
  type ModelRef,
  ModelRefSchema,
  type OtherRunsCost,
  OtherRunsCostSchema,
  RoleCostSchema,
  type Run,
  type RunChange,
  type SpendBucket,
  SpendBucketSchema,
  type Turn,
  TurnStatus,
  type Usage,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import {isLive} from '../../state/orchestrator/fleet.js';
import {applyChange} from '../../state/orchestrator/fold.js';
import {emptyState} from '../../state/orchestrator/state.js';
import {ms} from '../../state/orchestrator/time.js';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
/** The widths a series is cut in, so it stays short however long the window is. */
const LADDER_MS = [MINUTE, 5 * MINUTE, 15 * MINUTE, HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, 24 * HOUR];
const MAX_BUCKETS = 64;
const MAX_RUNS = 32;
const MAX_NAME_CHARS = 120;

/** One finished turn with what it belongs to. */
interface Counted {
  readonly agent: Agent;
  readonly turn: Turn;
  readonly endMs: number;
}

const KINDS = [
  'inputTokens',
  'cacheReadTokens',
  'cacheCreationTokens',
  'outputTokens',
  'reasoningTokens',
] as const;
type TokenKind = (typeof KINDS)[number];

/** What a group of turns has cost: the figures every list of the view carries. */
interface Tally {
  turns: number;
  unpriced: number;
  tokens: Record<TokenKind, bigint | undefined>;
  costUsd: number | undefined;
}

const newTally = (): Tally => ({
  turns: 0,
  unpriced: 0,
  tokens: {
    inputTokens: undefined,
    cacheReadTokens: undefined,
    cacheCreationTokens: undefined,
    outputTokens: undefined,
    reasoningTokens: undefined,
  },
  costUsd: undefined,
});

/** Add `part` into `into`: a turn is a tally of one. */
function merge(into: Tally, part: Tally): void {
  into.turns += part.turns;
  into.unpriced += part.unpriced;
  for (const kind of KINDS) {
    const tokens = part.tokens[kind];
    if (tokens !== undefined) into.tokens[kind] = (into.tokens[kind] ?? 0n) + tokens;
  }
  if (part.costUsd !== undefined) into.costUsd = (into.costUsd ?? 0) + part.costUsd;
}

function tallyOfTurn(usage: Usage | undefined): Tally {
  const tally = newTally();
  tally.turns = 1;
  tally.unpriced = usage?.costUsd === undefined ? 1 : 0;
  tally.costUsd = usage?.costUsd;
  for (const kind of KINDS) tally.tokens[kind] = usage?.[kind];
  return tally;
}

const tallyOf = (turns: readonly Counted[]): Tally => {
  const tally = newTally();
  for (const each of turns) merge(tally, tallyOfTurn(each.turn.usage));
  return tally;
};

const totalTokens = (tally: Tally): bigint =>
  KINDS.reduce((sum, kind) => sum + (tally.tokens[kind] ?? 0n), 0n);

const usageOfTally = (tally: Tally): Usage =>
  create(UsageSchema, {
    ...tally.tokens,
    ...(tally.costUsd === undefined ? {} : {costUsd: tally.costUsd}),
  });

/** A named group of turns: a run, a role or a model. */
interface Group<K> {
  readonly name: string;
  readonly key: K;
  readonly tally: Tally;
}

/** Group `turns` by `key`, most costly group first and a group with no priced turn after, by name. */
function groupsOf<K>(
  turns: readonly Counted[],
  key: (each: Counted) => K,
  name: (key: K) => string,
): Group<K>[] {
  const byName = new Map<string, Group<K>>();
  for (const each of turns) {
    const k = key(each);
    const group = byName.get(name(k)) ?? {name: name(k), key: k, tally: newTally()};
    byName.set(name(k), group);
    merge(group.tally, tallyOfTurn(each.turn.usage));
  }
  return byCost([...byName.values()]);
}

function byCost<T extends {name: string; tally: Tally}>(groups: T[]): T[] {
  return groups.sort((a, b) => {
    const left = a.tally.costUsd;
    const right = b.tally.costUsd;
    if (left !== undefined && right !== undefined && left !== right) return right - left;
    if ((left === undefined) !== (right === undefined)) return left === undefined ? 1 : -1;
    return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  });
}

const modelOf = (agent: Agent): ModelRef =>
  create(ModelRefSchema, {harness: agent.harness, model: agent.model});
const modelName = (model: ModelRef): string => `${model.harness}|${model.model}`;

function foldedTurns(changes: readonly RunChange[]) {
  const state = changes.reduce(applyChange, emptyState);
  const turns: Counted[] = Object.values(state.turns).flatMap(turn => {
    const agent = state.agents[turn.agentId];
    const endMs = ms(turn.endedAt);
    const counted = agent !== undefined && endMs !== null && turn.status !== TurnStatus.RUNNING;
    return counted ? [{agent, turn, endMs}] : [];
  });
  const runs = Object.values(state.runs).sort((a, b) => (a.id < b.id ? -1 : 1));
  return {runs, agents: Object.values(state.agents), turns};
}

/** The smallest width of the ladder that keeps the window within the most buckets a series has. */
const widthFor = (spanMs: number): number =>
  LADDER_MS.find(width => Math.ceil(spanMs / width) <= MAX_BUCKETS) ??
  LADDER_MS[LADDER_MS.length - 1] ??
  HOUR;

const costOf = (turns: readonly Counted[]): number =>
  turns.reduce((sum, each) => sum + (each.turn.usage?.costUsd ?? 0), 0);

interface Window {
  readonly startMs: number;
  readonly asOfMs: number;
  readonly widthMs: number;
}

function spendBuckets(
  turns: readonly Counted[],
  models: readonly ModelRef[],
  window: Window,
): SpendBucket[] {
  const {startMs, asOfMs, widthMs} = window;
  const count = Math.max(1, Math.ceil((asOfMs - startMs) / widthMs));
  const placeOf = (endMs: number) =>
    Math.min(count - 1, Math.max(0, Math.floor((endMs - startMs) / widthMs)));
  return Array.from({length: count}, (_, place) => {
    const inBucket = turns.filter(
      each => each.turn.usage?.costUsd !== undefined && placeOf(each.endMs) === place,
    );
    const byModel = models.flatMap(model => {
      const costUsd = costOf(
        inBucket.filter(each => modelName(modelOf(each.agent)) === modelName(model)),
      );
      return costUsd > 0 ? [{model, costUsd}] : [];
    });
    return create(SpendBucketSchema, {
      start: timestampFromMs(startMs + place * widthMs),
      costUsd: costOf(inBucket),
      byModel,
    });
  });
}

function otherRunsOf(rest: readonly Group<Run>[]): OtherRunsCost {
  const sum = newTally();
  for (const group of rest) merge(sum, group.tally);
  // A run's turns are counted once each: `merge` sums the groups' counts as they are.
  return create(OtherRunsCostSchema, {
    runs: rest.length,
    turns: sum.turns,
    unpricedTurns: sum.unpriced,
    usage: usageOfTally(sum),
    totalTokens: totalTokens(sum),
  });
}

/** When the view is worked out: the clock while a run is live, else the latest end. */
function windowOf(runs: readonly Run[], nowMs: number, sinceMs: number | null): Window {
  const live = runs.some(run => isLive(run.status));
  const asOfMs =
    live || runs.length === 0 ? nowMs : Math.max(...runs.map(run => ms(run.endedAt) ?? nowMs));
  const startMs = sinceMs ?? Math.min(asOfMs, ...runs.map(run => ms(run.createdAt) ?? asOfMs));
  return {startMs, asOfMs, widthMs: widthFor(asOfMs - startMs)};
}

/**
 * The host cost view of the runs `changes` build, as of `nowMs` while a run is live and as of the
 * latest end once none is. With `sinceMs`, it follows the service contract: a run ended before the
 * window is absent, and only turns finished in the window contribute to its figures.
 */
export function servedHostCost(
  changes: readonly RunChange[],
  nowMs: number,
  sinceMs: number | null = null,
): HostCost {
  const folded = foldedTurns(changes);
  const runs = folded.runs.filter(run => {
    const endedAtMs = ms(run.endedAt);
    return sinceMs === null || endedAtMs === null || endedAtMs >= sinceMs;
  });
  const runIds = new Set(runs.map(run => run.id));
  const agents = folded.agents.filter(agent => runIds.has(agent.runId));
  const turns = folded.turns.filter(
    turn => runIds.has(turn.agent.runId) && (sinceMs === null || turn.endMs >= sinceMs),
  );
  const all = tallyOf(turns);
  const window = windowOf(runs, nowMs, sinceMs);
  const ofRun = new Map(
    groupsOf(
      turns,
      each => each.agent.runId,
      id => id,
    ).map(g => [g.name, g.tally]),
  );
  const lines: Group<Run>[] = byCost(
    runs.map(run => ({name: run.id, key: run, tally: ofRun.get(run.id) ?? newTally()})),
  );
  const listed = lines.slice(0, MAX_RUNS);
  const rest = lines.slice(MAX_RUNS);
  const models = groupsOf(turns, each => modelOf(each.agent), modelName);
  const lastHour = turns.filter(
    each => each.endMs >= window.asOfMs - HOUR && each.endMs <= window.asOfMs,
  );
  return create(HostCostSchema, {
    sequence: BigInt(changes.length),
    asOf: timestampFromMs(window.asOfMs),
    total: usageOfTally(all),
    runCount: runs.length,
    turns: all.turns,
    unpricedTurns: all.unpriced,
    runs: listed.map(({key: run, tally}) =>
      create(HostRunCostSchema, {
        runId: run.id,
        name: run.task.slice(0, MAX_NAME_CHARS),
        status: run.status,
        ...(run.maxCostUsd === undefined ? {} : {maxCostUsd: run.maxCostUsd}),
        turns: tally.turns,
        unpricedTurns: tally.unpriced,
        usage: usageOfTally(tally),
        totalTokens: totalTokens(tally),
      }),
    ),
    ...(rest.length === 0 ? {} : {otherRuns: otherRunsOf(rest)}),
    roles: groupsOf(turns, each => each.agent.role, String).map(({key: role, tally}) =>
      create(RoleCostSchema, {
        role,
        agents: agents.filter(agent => agent.role === role).length,
        turns: tally.turns,
        unpricedTurns: tally.unpriced,
        usage: usageOfTally(tally),
        totalTokens: totalTokens(tally),
      }),
    ),
    models: models.map(({key: model, tally}) =>
      create(ModelCostSchema, {
        model,
        turns: tally.turns,
        unpricedTurns: tally.unpriced,
        usage: usageOfTally(tally),
        totalTokens: totalTokens(tally),
      }),
    ),
    series: {
      start: timestampFromMs(window.startMs),
      width: {seconds: BigInt(window.widthMs / 1000), nanos: 0},
      buckets: spendBuckets(
        turns,
        models.map(group => group.key),
        window,
      ),
    },
    ...(all.costUsd === undefined ? {} : {lastHourUsd: costOf(lastHour)}),
    modelsTruncated: false,
  });
}
