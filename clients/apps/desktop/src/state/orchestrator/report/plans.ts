/**
 * The run's plans at a glance: the host's own count of them by status (`SearchState.plans`), and the
 * jobs the host ran that no run has claimed as its own (`SearchState.unattributed_jobs`). A plan's
 * status — Open, Chosen, Won, Parked, Rejected — is finer than the verdict the ideas table shows,
 * which folds Chosen into Open and Won into Kept, so this count keeps Chosen and Won apart. Nothing
 * is worked out here beyond the count by status: the status, the word for it and the jobs figure are
 * all the host's, read through `planStatus` and `jobsWithNoRun`.
 */
import {PlanStatus, type SearchState} from '@vibesys/orchestrator-proto';
import {jobsWithNoRun, planStatus} from '../search-views.js';

export interface PlansReport {
  /**
   * The run's plans counted by status, in a fixed status order and only the statuses present. Each
   * carries the host's word for its status, as `planStatus` serves it.
   */
  readonly tallies: readonly {
    readonly status: PlanStatus;
    readonly label: string;
    readonly count: number;
  }[];
  readonly total: number;
  /** Jobs the host ran that no run has claimed as its own (`SearchState.unattributed_jobs`). */
  readonly withoutRun: number;
}

/** The order the statuses read in: open work first, then a plan chosen, won, parked or rejected. */
const STATUS_ORDER: readonly PlanStatus[] = [
  PlanStatus.OPEN,
  PlanStatus.CHOSEN,
  PlanStatus.WON,
  PlanStatus.PARKED,
  PlanStatus.REJECTED,
  PlanStatus.UNSPECIFIED,
];

/** The run's plans counted by status, or undefined while no search state has been served. */
export function plansReport(search: SearchState | undefined): PlansReport | undefined {
  if (search === undefined) return undefined;
  const rows = planStatus(search);
  const byStatus = new Map<PlanStatus, PlansReport['tallies'][number]>();
  for (const {plan, label} of rows) {
    const seen = byStatus.get(plan.status);
    byStatus.set(
      plan.status,
      seen === undefined
        ? {status: plan.status, label, count: 1}
        : {...seen, count: seen.count + 1},
    );
  }
  // The fixed order first, then any status the order does not name, so a new status is never dropped.
  const ordered = [
    ...STATUS_ORDER,
    ...[...byStatus.keys()].filter(status => !STATUS_ORDER.includes(status)),
  ];
  const tallies = ordered.flatMap(status => {
    const tally = byStatus.get(status);
    return tally === undefined ? [] : [tally];
  });
  return {tallies, total: rows.length, withoutRun: jobsWithNoRun(search)};
}
