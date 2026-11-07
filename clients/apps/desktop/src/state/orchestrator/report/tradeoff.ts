/**
 * The trade-off of a task with two or more objectives: every candidate measured on all of them, and
 * which ones the host says no other candidate beats. The frontier is the host's: this reads its
 * marks and counts them, and never compares one candidate with another.
 */
import type {SearchState} from '@vibesys/orchestrator-proto';
import {ms} from '../time.js';
import {type Better, betterOf} from './objectives.js';

export interface TradeoffPointView {
  readonly id: string;
  readonly commit: string;
  readonly atMs: number;
  /** One value per objective, in the order of `TradeoffReport.objectives`. */
  readonly values: readonly number[];
  readonly onFrontier: boolean;
}

export interface TradeoffReport {
  readonly objectives: readonly {readonly name: string; readonly better: Better}[];
  /** Oldest first, as served. */
  readonly points: readonly TradeoffPointView[];
  readonly frontierCount: number;
}

/** The trade-off the host serves, or undefined for a task with fewer than two objectives. */
export function tradeoffReport(search: SearchState): TradeoffReport | undefined {
  const tradeoff = search.tradeoff;
  if (tradeoff === undefined || tradeoff.objectives.length < 2) return undefined;
  const points = tradeoff.points.map(point => ({
    id: point.jobId,
    commit: point.commit,
    atMs: ms(point.at) ?? 0,
    values: point.values,
    onFrontier: point.onFrontier,
  }));
  return {
    objectives: tradeoff.objectives.map(objective => ({
      name: objective.name,
      better: betterOf(objective.direction),
    })),
    points,
    frontierCount: points.filter(point => point.onFrontier).length,
  };
}
