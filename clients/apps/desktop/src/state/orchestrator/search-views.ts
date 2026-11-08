/**
 * Pure reads of CLI-owned facts. No verdict or headline is inferred from agent text.
 *
 * A verdict's kind and a plan's status are the CLI's own enums, carried on fields the contract does
 * not pin to a known value (unlike the orchestrator's own statuses), so a newer host can name a kind
 * or status this build does not have. A label falls back to the unspecified one rather than showing
 * nothing for it.
 */
import {
  type Direction,
  JobState,
  type ObjectiveBound,
  oneofIs,
  PlanStatus,
  type SearchState,
  VerdictKind,
} from '@vibesys/orchestrator-proto';
import {ms} from './time.js';

// Typed by a number so a kind outside the known set reads as undefined (a newer host can send one);
// `satisfies` still makes a kind added to the enum a compile error here.
const verdictWords: Readonly<Record<number, string>> = {
  [VerdictKind.UNSPECIFIED]: 'Not judged',
  [VerdictKind.BETTER]: 'Better',
  [VerdictKind.WORSE]: 'Worse',
  [VerdictKind.WITHIN_NOISE]: 'Within noise',
  [VerdictKind.INCONCLUSIVE]: 'Inconclusive',
  [VerdictKind.TRADEOFF]: 'Tradeoff',
} satisfies Record<VerdictKind, string>;

/** Measurements and the daemon's verdict, only while it compares against the current head. */
export function candidates(search: SearchState): {
  result: SearchState['results'][number];
  judgment: SearchState['judgments'][number] | undefined;
  label: string;
  stale: boolean;
}[] {
  const head = search.head?.state;
  const against = oneofIs(head, 'current', {current: true, none: true})
    ? head.value.head
    : head?.value?.head;
  return search.results
    .filter(result => oneofIs(result.outcome, 'measure', {check: true, measure: true}))
    .map(result => {
      const judgment = search.judgments.find(row => row.commit === result.commit);
      const current = judgment !== undefined && judgment.against === against;
      const outcome = current ? judgment.outcome : undefined;
      const verdict = oneofIs(outcome, 'comparison', {comparison: true, refused: true})
        ? outcome.value.verdict
        : undefined;
      return {
        result,
        judgment,
        label:
          verdict === undefined
            ? oneofIs(outcome, 'refused', {comparison: true, refused: true})
              ? 'Comparison unavailable'
              : 'Not judged'
            : (verdictWords[verdict.kind] ?? 'Not judged'),
        stale: judgment !== undefined && !current,
      };
    });
}

/** Active jobs with explicit elapsed time and typed trouble, never a guessed tool-call start. */
export function jobQueue(
  search: SearchState,
  nowMs: number,
): {
  job: SearchState['jobs'][number];
  elapsedMs: number;
  trouble: SearchState['troubles'][number] | undefined;
}[] {
  return search.jobs
    .filter(job => job.state === JobState.QUEUED || job.state === JobState.RUNNING)
    .map(job => ({
      job,
      elapsedMs: Math.max(0, nowMs - (ms(job.startedAt) ?? ms(job.submittedAt) ?? nowMs)),
      trouble: search.troubles.find(trouble => trouble.jobId === job.id),
    }));
}

// Typed by a number for the same reason as `verdictWords`: an unknown status reads as undefined.
const planWords: Readonly<Record<number, string>> = {
  [PlanStatus.UNSPECIFIED]: 'Unknown',
  [PlanStatus.OPEN]: 'Open',
  [PlanStatus.CHOSEN]: 'Chosen',
  [PlanStatus.WON]: 'Won',
  [PlanStatus.PARKED]: 'Parked',
  [PlanStatus.REJECTED]: 'Rejected',
} satisfies Record<PlanStatus, string>;

export function planStatus(
  search: SearchState,
): {plan: SearchState['plans'][number]; label: string}[] {
  return search.plans.map(plan => ({plan, label: planWords[plan.status] ?? 'Unknown'}));
}

export function jobsWithNoRun(search: SearchState): number {
  return search.unattributedJobs;
}

/** One declaration of a bound, as the agent made it. Nothing here is derived. */
interface BoundVersionView {
  readonly seq: bigint;
  readonly value: number;
  readonly unit: string;
  /** How the agent derived the bound: the provenance a reader judges it by. */
  readonly note: string;
  /** Empty for a declaration made outside a run. */
  readonly agent: string;
  readonly atMs: number | null;
}

/**
 * The performance bound of one objective: the bound that stands, every version of it (a chart draws
 * the bound as it stood at each moment) and how close the head, and the best the run measured, are.
 * `headValue`, `percentOfBound` and `bestPercentOfBound` are the orchestrator's: the app shows them
 * and never divides, so they are absent exactly when the orchestrator sent none.
 */
export interface ObjectiveBoundsView {
  readonly objective: string;
  readonly direction: Direction;
  readonly current: BoundVersionView;
  /** Oldest first, ending with `current`. */
  readonly history: readonly BoundVersionView[];
  readonly headValue: number | undefined;
  readonly percentOfBound: number | undefined;
  /** How close the best measured value is to the bound in percent; the head is not always the best. */
  readonly bestPercentOfBound: number | undefined;
}

function boundVersion(version: ObjectiveBound): BoundVersionView {
  return {
    seq: version.seq,
    value: version.value,
    unit: version.unit,
    note: version.note,
    agent: version.origin?.agent ?? '',
    atMs: ms(version.origin?.at),
  };
}

/** The bounds the run's agents declared, by objective name as the orchestrator serves them. */
export function objectiveBounds(search: SearchState): readonly ObjectiveBoundsView[] {
  return search.bounds.flatMap(entry => {
    const history = entry.versions.map(boundVersion);
    const current = history.at(-1);
    return current === undefined
      ? []
      : [
          {
            objective: entry.objective,
            direction: entry.direction,
            current,
            history,
            headValue: entry.headValue,
            percentOfBound: entry.percentOfBound,
            bestPercentOfBound: entry.bestPercentOfBound,
          },
        ];
  });
}
