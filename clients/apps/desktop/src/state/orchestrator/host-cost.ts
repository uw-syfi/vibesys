/**
 * What every run the host holds has cost, as the host works it out (`HostCost` in
 * `proto/vibesys/orchestrator/v1/cost.proto`). The host owns every number: the total, the runs ranked
 * by cost with the rest summed, the split by type of agent and by model, the buckets of spend over
 * time and the rate of the last hour. A screen shows these and computes none of them, so this file
 * only changes their shape (bigints and timestamps become numbers) and keeps the latest view of each
 * window. It adds nothing up and orders nothing: every list is in the order the host sent it.
 *
 * Its lists are the run view's (`cost.ts`): the same types of agent, models and series, read by the
 * same code. Each view replaces the one before it, unless it is older than the one held
 * (`applyHostCost` in `fold.ts`). A message that lacks a field the contract requires is not a view
 * with a default in its place: the read names the field (`readHostCost`), and the feed fails with it.
 */
import type {HostCost, HostRunCost, OtherRunsCost, RunStatus} from '@vibesys/orchestrator-proto';
import {
  type CostUsage,
  instantMs,
  MissingField,
  type ModelCostView,
  modelCostView,
  type RoleCostView,
  roleView,
  type SpendSeriesView,
  seriesView,
  usageView,
} from './cost.js';

/**
 * The span a host cost view counts: every turn the host holds, or the turns that finished at or after
 * an instant, in whole milliseconds. It is also the key the view is held under, so two windows never
 * show each other's numbers.
 */
export type HostCostWindow = 'all' | `since:${number}`;

/** One rolling day in milliseconds: the Cost destination fixes this window when it opens. */
export const COST_DAY_MS = 24 * 60 * 60 * 1000;

/** The window of the turns that finished at or after `sinceMs`. */
export const sinceWindow = (sinceMs: number): HostCostWindow => `since:${Math.trunc(sinceMs)}`;

/** The exact 24-hour window ending when the Cost destination opened. */
export const lastDayWindow = (openedAtMs: number): HostCostWindow =>
  sinceWindow(openedAtMs - COST_DAY_MS);

/** Where the window begins, or null when it counts everything the host holds. */
export const windowSinceMs = (window: HostCostWindow): number | null =>
  window === 'all' ? null : Number(window.slice('since:'.length));

/** One run, as a line of the host's list of runs by cost. */
export interface HostRunCostView {
  readonly runId: string;
  /** What the run is called: its task, cut by the host when it is long. */
  readonly name: string;
  readonly status: RunStatus;
  /** The run's cost limit, or null when it has none. */
  readonly maxCostUsd: number | null;
  /** The finished turns the window counts of the run. */
  readonly turns: number;
  /** Turns that reported tokens and no cost: the run's cost leaves them out. */
  readonly unpricedTurns: number;
  readonly usage: CostUsage;
  /** All the run's tokens, as the host added them up. */
  readonly totalTokens: number;
}

/** The runs the list leaves out, added up by the host. */
export interface OtherRunsCostView {
  /** How many runs are summed here. */
  readonly runs: number;
  readonly turns: number;
  readonly unpricedTurns: number;
  readonly usage: CostUsage;
  readonly totalTokens: number;
}

export interface HostCostView {
  /** The log sequence the view is current to. */
  readonly sequence: bigint;
  /** The instant the view is worked out at: the host's clock while a counted run lives, the last end after. */
  readonly asOfMs: number;
  readonly total: CostUsage;
  /** How many runs the view counts: those in `runs` and those in `otherRuns`. */
  readonly runCount: number;
  readonly turns: number;
  /** How many finished turns have tokens and no cost; any is a reason to call the cost a floor. */
  readonly unpricedTurns: number;
  /** The most costly runs, most costly first; a run with no priced turn comes after them. */
  readonly runs: readonly HostRunCostView[];
  /** The runs `runs` leaves out, summed; null when every counted run is listed. */
  readonly otherRuns: OtherRunsCostView | null;
  /** Most costly first; a type with no priced turn comes last. */
  readonly roles: readonly RoleCostView[];
  /** Most costly first; a model with no priced turn comes last. */
  readonly models: readonly ModelCostView[];
  readonly series: SpendSeriesView;
  /** What the turns that finished in the hour before `asOfMs` cost; null when none is priced. */
  readonly lastHourUsd: number | null;
  /** The host left a model out of the lists of models, or cut a model's name; every other figure is whole. */
  readonly modelsTruncated: boolean;
}

const runView = (run: HostRunCost): HostRunCostView => ({
  runId: run.runId,
  name: run.name,
  status: run.status,
  maxCostUsd: run.maxCostUsd ?? null,
  turns: run.turns,
  unpricedTurns: run.unpricedTurns,
  usage: usageView(run.usage, 'runs.usage'),
  totalTokens: Number(run.totalTokens),
});

const otherView = (others: OtherRunsCost): OtherRunsCostView => ({
  runs: others.runs,
  turns: others.turns,
  unpricedTurns: others.unpricedTurns,
  usage: usageView(others.usage, 'other_runs.usage'),
  totalTokens: Number(others.totalTokens),
});

/** What reading a message came to: the view a screen shows, or the required field it lacks. */
export type HostCostRead =
  | {readonly _tag: 'View'; readonly view: HostCostView}
  | {readonly _tag: 'Missing'; readonly field: string};

/**
 * The view a screen reads, from the message the host sent. The contract's validation rules require
 * those fields, so a message that lacks one comes from a host that broke the contract; the read names
 * the field and shows no zero or empty name in its place.
 */
export function readHostCost(cost: HostCost): HostCostRead {
  try {
    return {
      _tag: 'View',
      view: {
        sequence: cost.sequence,
        asOfMs: instantMs(cost.asOf, 'as_of'),
        total: usageView(cost.total, 'total'),
        runCount: cost.runCount,
        turns: cost.turns,
        unpricedTurns: cost.unpricedTurns,
        runs: cost.runs.map(runView),
        otherRuns: cost.otherRuns === undefined ? null : otherView(cost.otherRuns),
        roles: cost.roles.map(roleView),
        models: cost.models.map(modelCostView),
        series: seriesView(cost.series),
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
