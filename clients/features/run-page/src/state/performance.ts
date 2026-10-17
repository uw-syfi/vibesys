/**
 * The performance chart's data fold: every idea's measured value on the run clock, what each idea's
 * result made of it (passed, failed, or not decided yet), and the best passed value so far as a
 * step line. An idea that sets a new best sits on the line; one that passed but did not beat the
 * best so far (or the input) does not. Values are the framework's gate readings only.
 *
 * A pure fold over the candidates in measurement order; the strip chart view only projects it to
 * drawing data.
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import type {Direction} from './run-model.js';

/** What an idea's own measurement made of it. */
export type IdeaResult = 'passed' | 'failed' | 'pending';

/** One measured idea. */
export interface PerformancePoint {
  readonly id: string;
  readonly title: string;
  /** Run-clock seconds when the measurement ended. */
  readonly at: number;
  readonly value: number;
  readonly result: IdeaResult;
  /** Passed and better than every passed value before it: it sits on the step line. */
  readonly best: boolean;
}

/** The best passed value from `at` on (until the next step). */
interface BestStep {
  readonly at: number;
  readonly value: number;
}

export interface PerformanceFold {
  /** Every measured idea, in the order measured. */
  readonly points: readonly PerformancePoint[];
  /** The best so far: one step per new best, in order, each better than the one before. */
  readonly best: readonly BestStep[];
}

/**
 * What the idea's own result made of it: passed or failed (the workstream's verdict, which a
 * measured idea slower than the input still passes), or not decided while it runs. A value alone
 * decides nothing: the gate may read before the idea settles.
 */
function resultOf(item: Candidate): IdeaResult {
  return item.outcome === 'running' ? 'pending' : item.outcome;
}

/** Whether `value` beats `than` where `direction` says which way is better. */
function beats(value: number, than: number, direction: Direction): boolean {
  return direction === 'min' ? value < than : value > than;
}

/**
 * The fold of `candidates` measured so far, against the input's value `input` (null when unknown):
 * the input stands as the best until a passed idea beats it, as the search's adoption does.
 */
export function performanceFold(
  candidates: readonly Candidate[],
  direction: Direction,
  input: number | null,
): PerformanceFold {
  const measured = candidates
    .filter(item => item.measurement?.metricValue != null)
    .map(item => ({item, at: item.measurement?.at ?? item.startedAt}))
    .sort((a, b) => a.at - b.at);
  const points: PerformancePoint[] = [];
  const best: BestStep[] = [];
  let standing = input;
  for (const {item, at} of measured) {
    const value = item.measurement?.metricValue as number;
    const result = resultOf(item);
    const sets = result === 'passed' && (standing === null || beats(value, standing, direction));
    if (sets) {
      best.push({at, value});
      standing = value;
    }
    points.push({id: item.id, title: item.title, at, value, result, best: sets});
  }
  return {points, best};
}
