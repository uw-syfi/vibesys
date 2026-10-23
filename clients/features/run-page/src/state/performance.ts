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
import type {Direction} from './direction.js';

/** What an idea's own measurement made of it. */
export type IdeaResult = 'passed' | 'failed' | 'pending';

/** One measured idea. */
export interface PerformancePoint {
  readonly id: string;
  /** Distinct samples may belong to the same idea. */
  readonly key?: string;
  /** A metric can exist without an idea to open. */
  readonly unowned?: boolean;
  readonly title: string;
  /** Run-clock seconds when the idea was first measured (see `measuredAt`). */
  readonly at: number;
  readonly value: number;
  readonly result: IdeaResult;
  readonly outcomeWord?: string;
  /** Passed and better than every passed value before it: it sits on the step line. */
  readonly best: boolean;
}

/** The best passed value from `at` on (until the next step). */
interface BestStep {
  readonly at: number;
  readonly value: number;
}

export interface PerformanceFold {
  readonly value?: number | undefined;
  readonly input?: number | undefined;
  readonly refusal?: string | undefined;
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
 * Whether the framework's comparison of the idea with its base leaves room for a new best: a
 * result within noise of its base, or worse than it, is no new best however its value reads.
 * An idea with no comparison (or one too early to tell) stands on its value.
 */
function holdsAgainstBase(item: Candidate): boolean {
  const verdict = item.measurement?.against?.verdict;
  return verdict !== 'within_noise' && verdict !== 'worse';
}

/**
 * When the idea was first measured: the end of its earliest finished gate, never later than the
 * measurement it shows. A run resumed on new code asks a gate again (`measure.2`) for an idea it
 * had measured before, so the gate its measurement reads can end hours after the idea first had
 * its value; the idea belongs where it first had it, or the best so far would differ before and
 * after the resume.
 */
function measuredAt(item: Candidate): number {
  const shown = item.measurement?.at ?? item.startedAt;
  const finished = item.attempts
    .flatMap(attempt => attempt.steps)
    .filter(step => step.kind === 'gate' && step.status === 'done' && step.endedAt !== null)
    .map(step => step.endedAt as number);
  return Math.min(shown, ...finished);
}

/** The input's value: any measurement's value over its ratio to the input; null when unknown. */
export function inputValueOf(candidates: readonly Candidate[]): number | null {
  for (const item of candidates) {
    const value = item.measurement?.metricValue ?? null;
    const vs = item.measurement?.vsBaseline ?? null;
    if (value !== null && vs !== null && vs !== 0) return value / vs;
  }
  return null;
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
    .map(item => ({item, at: measuredAt(item)}))
    .sort((a, b) => a.at - b.at);
  const points: PerformancePoint[] = [];
  const best: BestStep[] = [];
  let standing = input;
  for (const {item, at} of measured) {
    const value = item.measurement?.metricValue as number;
    const result = resultOf(item);
    const sets =
      result === 'passed' &&
      holdsAgainstBase(item) &&
      (standing === null || beats(value, standing, direction));
    if (sets) {
      best.push({at, value});
      standing = value;
    }
    points.push({id: item.id, title: item.title, at, value, result, best: sets});
  }
  return {points, best};
}
