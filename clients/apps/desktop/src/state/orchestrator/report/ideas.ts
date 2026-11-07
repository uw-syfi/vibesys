/**
 * The run's ideas and workstreams as the orchestrator serves them (`SearchState.ideas`,
 * `workstream_views`, `workstreams`, `unplanned`), joined to the plans they describe and shaped for
 * a table: a row is a plan's own record (number, title, workstream, origin, times) beside its
 * `IdeaView` (verdict, candidates, gains). Nothing is worked out here: the verdict, the counts and the
 * gains are the host's. The only things this file computes are display filters (`ideasInWindow`,
 * `ideasOfVerdict`, `ideasInRun`), which keep or drop rows by facts already on them.
 */
import {
  IdeaVerdict,
  type ObjectiveGain,
  type SearchState,
  type WorkstreamStatus,
} from '@vibesys/orchestrator-proto';
import {ms} from '../time.js';

/** What an idea reached on one objective, against where the run began: as served. */
export interface GainView {
  readonly metric: string;
  /** Best over baseline, oriented so that more than 1 is better. */
  readonly ratio: number;
  readonly bestValue: number;
  readonly baselineValue: number;
}

const gainOf = (gain: ObjectiveGain): GainView => ({
  metric: gain.metric,
  ratio: gain.ratio,
  bestValue: gain.bestValue,
  baselineValue: gain.baselineValue,
});

export type Verdict = 'open' | 'kept' | 'rejected' | 'parked';

const VERDICTS = {
  [IdeaVerdict.UNSPECIFIED]: 'open',
  [IdeaVerdict.OPEN]: 'open',
  [IdeaVerdict.KEPT]: 'kept',
  [IdeaVerdict.REJECTED]: 'rejected',
  [IdeaVerdict.PARKED]: 'parked',
} as const satisfies Record<IdeaVerdict, Verdict>;

export interface IdeaRow {
  readonly planNumber: number;
  readonly title: string;
  readonly verdict: Verdict;
  /** The workstream's number, 0 for none. */
  readonly workstream: number;
  /** The workstream's title; null for an idea with none or whose workstream was left out. */
  readonly workstreamTitle: string | null;
  /** One for each objective an idea's candidate was measured on, in the order served. */
  readonly gains: readonly GainView[];
  /** The measure jobs that name the plan, oldest first: the idea's chart points. */
  readonly candidateJobIds: readonly string[];
  /** The agent that made the plan, as it said so; empty when unrecorded. */
  readonly agent: string;
  readonly createdMs: number | null;
  readonly updatedMs: number | null;
  /** Made, or its status changed, in this run. */
  readonly inRun: boolean;
}

export interface WorkstreamRow {
  /** 0 holds the ideas that name none. */
  readonly number: number;
  readonly title: string | null;
  readonly status: WorkstreamStatus | undefined;
  readonly note: string;
  readonly kept: number;
  readonly rejected: number;
  readonly open: number;
  readonly parked: number;
  readonly gains: readonly GainView[];
}

export interface IdeasReport {
  /** Newest plan first. */
  readonly ideas: readonly IdeaRow[];
  /** The run's ideas counted by workstream, as served (ascending number). */
  readonly workstreams: readonly WorkstreamRow[];
  /** Measured candidates that name no plan: the baseline and deliberate `--no-plan` ones. */
  readonly unplanned: number;
  /** The host left some ideas or workstreams out to fit. */
  readonly truncated: boolean;
  /** The plans each measure job names, by job id: what a chart point links to. */
  readonly plansOfJob: ReadonlyMap<string, readonly number[]>;
}

/** The ideas of a run, or undefined while no search state has been served. */
export function ideasReport(search: SearchState | undefined): IdeasReport | undefined {
  if (search === undefined) return undefined;
  const views = new Map(search.ideas.map(view => [view.planNumber, view]));
  const streams = new Map(search.workstreams.map(stream => [stream.number, stream]));
  const ideas = search.plans
    .map((plan): IdeaRow => {
      const view = views.get(plan.number);
      return {
        planNumber: plan.number,
        title: plan.title,
        verdict: VERDICTS[view?.verdict ?? IdeaVerdict.UNSPECIFIED],
        workstream: plan.workstream,
        workstreamTitle: streams.get(plan.workstream)?.title ?? null,
        gains: (view?.best ?? []).map(gainOf),
        candidateJobIds: view?.candidateJobIds ?? [],
        agent: plan.origin?.agent ?? '',
        createdMs: ms(plan.createdAt),
        updatedMs: ms(plan.updatedAt),
        inRun: view?.inRun ?? false,
      };
    })
    .sort((a, b) => b.planNumber - a.planNumber);
  const workstreams = search.workstreamViews.map(
    (view): WorkstreamRow => ({
      number: view.number,
      title: streams.get(view.number)?.title ?? null,
      status: streams.get(view.number)?.status,
      note: streams.get(view.number)?.note ?? '',
      kept: view.kept,
      rejected: view.rejected,
      open: view.open,
      parked: view.parked,
      gains: view.best.map(gainOf),
    }),
  );
  return {
    ideas,
    workstreams,
    unplanned: search.unplanned,
    truncated: search.truncated,
    plansOfJob: new Map(search.jobs.map(job => [job.id, job.plans])),
  };
}

/**
 * The plans a Catch me up check-in counts for its window, by number: the host's own membership
 * (`WindowFacts.plans_created` and `plans_changed`), the same one its Created and Changed counts
 * are the sizes of.
 */
export interface IdeaWindow {
  readonly plans: readonly number[];
}

/** Display filter: the ideas made in this run, or worked on in it. */
export const ideasInRun = (ideas: readonly IdeaRow[]): readonly IdeaRow[] =>
  ideas.filter(idea => idea.inRun);

/** Display filter: the ideas the host gave `verdict`. */
export const ideasOfVerdict = (ideas: readonly IdeaRow[], verdict: Verdict): readonly IdeaRow[] =>
  ideas.filter(idea => idea.verdict === verdict);

/** Display filter: the ideas whose plan the check-in counted (the host's membership, not a time test). */
export const ideasInWindow = (
  ideas: readonly IdeaRow[],
  window: IdeaWindow,
): readonly IdeaRow[] => {
  const counted = new Set(window.plans);
  return ideas.filter(idea => counted.has(idea.planNumber));
};
