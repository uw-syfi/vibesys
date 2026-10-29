/** Project core's decisions into display values, without selecting or comparing readings. */
import {IdeaOutcome, type RunHead} from '@vibesys/proto';
import type {StoreView} from './types.js';

export interface PerformanceDecision {
  readonly result: 'passed' | 'failed' | 'pending';
  readonly word: string;
  readonly best: boolean;
}
export interface PerformanceReading {
  readonly points: ReadonlyMap<bigint, PerformanceDecision>;
  readonly value: number | undefined;
  readonly input: number | undefined;
  readonly refusal: string | undefined;
}
const RESULTS: Partial<Record<IdeaOutcome, PerformanceDecision['result']>> = {
  [IdeaOutcome.FAILED]: 'failed',
  [IdeaOutcome.KEPT]: 'passed',
  [IdeaOutcome.NOT_KEPT]: 'passed',
  [IdeaOutcome.WITHIN_NOISE]: 'passed',
  [IdeaOutcome.WORSE_THAN_INPUT]: 'passed',
};
const WORDS: Record<IdeaOutcome, string> = {
  [IdeaOutcome.UNSPECIFIED]: 'Outcome unavailable',
  [IdeaOutcome.INPUT]: 'Input',
  [IdeaOutcome.UNATTRIBUTED]: 'Unattributed',
  [IdeaOutcome.PENDING]: 'Pending',
  [IdeaOutcome.FAILED]: 'Failed',
  [IdeaOutcome.KEPT]: 'Kept',
  [IdeaOutcome.NOT_KEPT]: 'Not kept',
  [IdeaOutcome.WITHIN_NOISE]: 'Within noise',
  [IdeaOutcome.WORSE_THAN_INPUT]: 'Worse than input',
};

export function readPerformance(
  view: StoreView,
  head: RunHead | undefined,
): ReadonlyMap<string, PerformanceReading> {
  if (head?.committedSeq !== view.seq || !head.performanceStated) return new Map();
  return new Map(
    head.performance.map(series => [
      series.series,
      {
        value: series.bestValue,
        input: series.inputValue,
        refusal: series.refusal === undefined ? undefined : 'Unknown objective direction',
        points: new Map(
          series.points.map(point => [
            point.measurementSeq,
            {
              result: RESULTS[point.outcome] ?? 'pending',
              word: WORDS[point.outcome],
              best: point.newBest,
            },
          ]),
        ),
      },
    ]),
  );
}
