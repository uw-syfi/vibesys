/** The Work tab's slice of the page state: which outcomes it shows and what it searches for. */

export type WorkOutcome = 'all' | 'accepted' | 'rejected' | 'running';
export const WORK_OUTCOMES: readonly WorkOutcome[] = ['all', 'accepted', 'rejected', 'running'];

export interface WorkFilter {
  readonly query: string;
  readonly outcome: WorkOutcome;
}

export const WORK_FILTER_ALL: WorkFilter = {query: '', outcome: 'all'};
