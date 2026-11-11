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

/** The verdicts in the order a person reads them: the ideas that stood, then what is open, then the rest. */
export const VERDICT_ORDER: readonly Verdict[] = ['kept', 'open', 'rejected', 'parked'];

/** How many of `ideas` the host gave each verdict. */
export function verdictCounts(ideas: readonly IdeaRow[]): Readonly<Record<Verdict, number>> {
  const counts: Record<Verdict, number> = {kept: 0, open: 0, rejected: 0, parked: 0};
  for (const idea of ideas) counts[idea.verdict] += 1;
  return counts;
}

/**
 * Display filter: the ideas a search matches, by the words a person reads in a row: the title, the
 * workstream's title or the idea's number (`12` or `#12`). Every word of the search must be found
 * in one of them, in any case; no search keeps every idea.
 */
export function ideasMatching(ideas: readonly IdeaRow[], search: string): readonly IdeaRow[] {
  const words = search
    .toLowerCase()
    .split(/\s+/)
    .filter(word => word !== '');
  if (words.length === 0) return ideas;
  return ideas.filter(idea => {
    const text = `#${idea.planNumber} ${idea.title} ${idea.workstreamTitle ?? ''}`.toLowerCase();
    return words.every(word => text.includes(word));
  });
}

/** The columns an ideas table can be ordered by. */
export type IdeaSortKey = 'id' | 'idea' | 'workstream' | 'verdict' | 'gain' | 'agent' | 'updated';

export type SortDirection = 'ascending' | 'descending';

/** The order a column is first sorted in when it is chosen: numbers and times newest or largest first. */
export const FIRST_DIRECTION: Readonly<Record<IdeaSortKey, SortDirection>> = {
  id: 'descending',
  idea: 'ascending',
  workstream: 'ascending',
  verdict: 'ascending',
  gain: 'descending',
  agent: 'ascending',
  updated: 'descending',
};

/** The sort a press of a header asks for: the same column turns over, another starts in its first order. */
export function nextSort(
  current: {readonly key: IdeaSortKey; readonly direction: SortDirection},
  pressed: IdeaSortKey,
): {readonly key: IdeaSortKey; readonly direction: SortDirection} {
  if (current.key !== pressed) return {key: pressed, direction: FIRST_DIRECTION[pressed]};
  return {key: pressed, direction: current.direction === 'ascending' ? 'descending' : 'ascending'};
}

type Sortable = number | string | null;

/** The value a row is ordered by in each column; null where it has none, which always comes last. */
const SORT_VALUE: Readonly<
  Record<IdeaSortKey, (row: IdeaRow, agentName: (agent: string) => string) => Sortable>
> = {
  id: row => row.planNumber,
  idea: row => row.title.toLowerCase(),
  workstream: row => row.workstreamTitle?.toLowerCase() ?? null,
  verdict: row => VERDICT_ORDER.indexOf(row.verdict),
  gain: row => row.gains[0]?.ratio ?? null,
  agent: (row, agentName) => (row.agent === '' ? null : agentName(row.agent).toLowerCase()),
  updated: row => row.updatedMs,
};

/** How two values of one column order, in the column's own direction; a value that is none comes last. */
function compared(a: Sortable, b: Sortable, sign: 1 | -1): number {
  if (a === null || b === null) return a === b ? 0 : a === null ? 1 : -1;
  return (a < b ? -1 : a > b ? 1 : 0) * sign;
}

/**
 * The ideas ordered by a column. A row with no value in the column comes last whichever way it is
 * ordered, and rows that tie keep the order they came in, so a table never shuffles under a live run.
 */
export function sortIdeas(
  ideas: readonly IdeaRow[],
  sort: {readonly key: IdeaSortKey; readonly direction: SortDirection},
  agentName: (agent: string) => string,
): readonly IdeaRow[] {
  const sign = sort.direction === 'ascending' ? 1 : -1;
  return ideas
    .map((row, at) => ({row, at, value: SORT_VALUE[sort.key](row, agentName)}))
    .sort((a, b) => compared(a.value, b.value, sign) || a.at - b.at)
    .map(each => each.row);
}
