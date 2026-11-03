/** Pure reads of CLI-owned facts. No verdict or headline is inferred from agent text. */
import {
  JobState,
  oneofIs,
  PlanStatus,
  type SearchState,
  VerdictKind,
} from '@vibesys/orchestrator-proto';
import {ms} from './time.js';

/** The official measurement of the current head, or its typed absence. */
export function resultSoFar(
  search: SearchState,
): NonNullable<SearchState['head']>['state'] | undefined {
  return search.head?.state;
}

const verdictWords = {
  [VerdictKind.UNSPECIFIED]: 'Not judged',
  [VerdictKind.BETTER]: 'Better',
  [VerdictKind.WORSE]: 'Worse',
  [VerdictKind.WITHIN_NOISE]: 'Within noise',
  [VerdictKind.INCONCLUSIVE]: 'Inconclusive',
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
            : verdictWords[verdict.kind],
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

const planWords = {
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
  return search.plans.map(plan => ({plan, label: planWords[plan.status]}));
}

export function jobsWithNoRun(search: SearchState): number {
  return search.unattributedJobs;
}
