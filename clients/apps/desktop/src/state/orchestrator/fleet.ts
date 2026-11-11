/**
 * The fleet: every run of every connected host as one row each, live runs first and then history,
 * with the filters of the Runs screen applied. A plain fold over what the hosts have said; it reads
 * no clock and starts no work, and it derives nothing the host serves: the report a run is summed up
 * by, the agents working, the best on each objective, a percent of a bound, a cost and a time are
 * shown as sent (the run's `RunSummary`), and absent exactly when the host sent none. Why a run wants
 * a look is read from the facts the host keeps for it (a stall, stuck jobs, a limit, a final report,
 * kept checkouts), never guessed from a duration. The screen formats.
 */
import {
  type ObjectiveStanding,
  type Run,
  RunLimit,
  RunStatus,
  type RunSummary,
  type SearchState,
  type TaskCard,
} from '@vibesys/orchestrator-proto';
import type {Freshness} from '../freshness.js';
import type {
  GenericRunEntry,
  GenericRunIndex,
  GenericRunIndexUnavailable,
  GenericRunState,
} from '../generic-run-index.js';
import type {HostStatus} from '../host-status.js';
import {inSetup} from '../setup/setup.js';
import {type ObjectiveReport, objectiveReports} from './report/objectives.js';
import type {OrchestratorState} from './state.js';
import {ms} from './time.js';

/** Which runs the list shows: all of them, those still working, or those that are over. */
export type FleetState = 'all' | 'live' | 'ended';

/** Every `FleetState`, in the order a control lists them. */
export const FLEET_STATES: readonly FleetState[] = ['all', 'live', 'ended'];

export interface FleetFilter {
  readonly state: FleetState;
  /** The `HostKey` of the one host shown, or null for every host. */
  readonly host: string | null;
}

export const NO_FLEET_FILTER: FleetFilter = {state: 'all', host: null};

/** The columns of the run list a person can sort by. */
export type FleetColumn = 'name' | 'status' | 'host' | 'best' | 'agents' | 'cost';

/** Every `FleetColumn`, in the order the list draws them. */
export const FLEET_COLUMNS: readonly FleetColumn[] = [
  'name',
  'status',
  'host',
  'best',
  'agents',
  'cost',
];

/** How the list is sorted: by one column, ascending or descending. No sort is the list's own order. */
export interface FleetSort {
  readonly column: FleetColumn;
  readonly direction: 'asc' | 'desc';
}

/**
 * What pressing a column's header does to the sort: sort by it ascending, then descending, then back
 * to the list's own order; pressing another column's header starts that one ascending.
 */
export function pressedSort(current: FleetSort | null, column: FleetColumn): FleetSort | null {
  if (current === null || current.column !== column) return {column, direction: 'asc'};
  return current.direction === 'asc' ? {column, direction: 'desc'} : null;
}

/** The key of the row of run `runId` on host `hostKey`: unique across hosts, and a route's own parts. */
export const fleetKey = (hostKey: string, runId: string): string => `${hostKey}/${runId}`;

/** One host the fleet reads, as the screen holds it. */
export interface FleetHostInput {
  /** The `HostKey`: what a route names. */
  readonly key: string;
  /** What the host is called on screen. */
  readonly label: string;
  readonly data: OrchestratorState;
  /** The host gateway's atomic generic run index, separate from the managed model above. */
  readonly runIndex: GenericRunIndex;
  readonly freshness: Freshness;
  /** The one status the host has on every screen. */
  readonly status: HostStatus;
}

/** A host as a row names it. */
export interface FleetHostRef {
  readonly key: string;
  readonly label: string;
  readonly freshness: Freshness;
  readonly status: HostStatus;
}

/**
 * The best the run measured on one objective, with the share of the declared bound when the host served
 * one. It is not the head's value: the head is the candidate the run last promoted, which on an
 * objective it was not promoted for need not be the best.
 */
export interface FleetObjective {
  readonly name: string;
  /** The best value measured, or null when the run measured nothing on this objective. */
  readonly value: number | null;
  /** The unit the agents declared the bound in; empty when no bound was declared. */
  readonly unit: string;
  /** How close the best is to the bound, in percent, as the host served it; null when it did not. */
  readonly percentOfBound: number | null;
}

/** Where the host says the run stands on one objective, as a row carries its best: absent as null. */
const objectiveOf = (standing: ObjectiveStanding): FleetObjective => ({
  name: standing.name,
  value: standing.bestValue ?? null,
  unit: standing.unit,
  percentOfBound: standing.bestPercentOfBound ?? null,
});

/**
 * Why a run wants the operator's look: each a fact the host keeps for the run. Only a live run can
 * stall, have stuck jobs, reach a limit or have finished its work; only a run that is over keeps
 * checkouts.
 */
export type FleetAttention =
  /** Nothing can move the run since `sinceMs`: no agent works, no message waits and no job runs. */
  | {readonly kind: 'stalled'; readonly sinceMs: number}
  /** Jobs of the run that are not final and that nothing is working on. */
  | {readonly kind: 'troubles'; readonly jobs: number}
  /** The run reached its time or its cost limit: its lead was told to finish. */
  | {readonly kind: 'limit'; readonly limit: 'time' | 'cost'; readonly atMs: number | null}
  /** The lead posted its final report and started no new work since. */
  | {readonly kind: 'final'}
  /** The run left working copies holding changes git does not have, for the operator to decide on. */
  | {readonly kind: 'kept'; readonly checkouts: number};

/** Every kind of `FleetAttention`, the most pressing first: the order a row lists its reasons in. */
export const ATTENTION_KINDS: readonly FleetAttention['kind'][] = [
  'stalled',
  'troubles',
  'limit',
  'final',
  'kept',
];

interface FleetRowBase {
  /** Unique across hosts: the host and the run, the same key a route is made from. */
  readonly key: string;
  readonly host: FleetHostRef;
  readonly runId: string;
  /** What the run is called: its task. */
  readonly name: string;
  /** What the host read of the run's task when it started; null for a run recorded before cards. */
  readonly card: TaskCard | null;
  readonly repository: string;
  /** Still working: starting, running or stopping. */
  readonly live: boolean;
  /** The section supported by typed state; unknown is shown only by the All filter. */
  readonly section: 'live' | 'history' | 'unknown';
  readonly startedMs: number | null;
  /** When the run left RUNNING; null while it is live. */
  readonly endedMs: number | null;
  /** The best the run measured on each objective, in the order the host serves them. */
  readonly objectives: readonly FleetObjective[];
  /** What every turn has cost so far, or null when no turn reported a cost. */
  readonly costUsd: number | null;
  /** The most the run may cost, or null for a run with no cost limit. */
  readonly budgetUsd: number | null;
  /**
   * Agents that are starting, in a turn or waiting on a subagent, as the host counts them; null until
   * the host has sent the run's summary.
   */
  readonly agentsRunning: number | null;
  /**
   * When the lead last posted a report about the whole run (never one that answered Catch me up), or
   * null when it has not.
   */
  readonly lastReportMs: number | null;
  /** The host could not serve the run's report: it is missing, not posted by no one. */
  readonly reportUnavailable: boolean;
  /** When the run reaches its time limit, or null for a run with none. */
  readonly deadlineMs: number | null;
  /** Why the run wants a look, in the order of `ATTENTION_KINDS`; empty when nothing does. */
  readonly attention: readonly FleetAttention[];
}

/** A row backed by the managed orchestrator model. */
export interface ManagedFleetRow extends FleetRowBase {
  readonly source: 'managed';
  /** The run is in its setup phase: a chat that makes its task, with no task yet. */
  readonly setup: boolean;
  readonly status: RunStatus;
}

/** A row backed only by the host gateway's generic run index. */
export interface GenericFleetRow extends FleetRowBase {
  readonly source: 'generic';
  readonly state: GenericRunState;
  readonly projectRoot: string | null;
  /** Whether this row came from the current atomic index or the last whole stale snapshot. */
  readonly indexFreshness: 'current' | 'stale';
  readonly needsUser: boolean;
}

export type FleetRow = ManagedFleetRow | GenericFleetRow;

/** Where one host's generic run-index stream stands. */
export type GenericDiscoveryStatus =
  | {readonly kind: 'opening'}
  | {readonly kind: 'current'}
  | {readonly kind: 'stale'}
  | {readonly kind: 'unavailable'; readonly reason: GenericRunIndexUnavailable};

/** The two independent discovery streams behind a host in the filter strip. */
export interface FleetDiscoveryStatus {
  readonly managed: Pick<FleetHostRef, 'freshness' | 'status'>;
  readonly generic: GenericDiscoveryStatus;
}

/** A host as the filter strip shows it, with its run count under the state filter. */
export interface FleetHostView {
  readonly key: string;
  readonly label: string;
  /** Per-source status: neither discovery stream is allowed to speak for the other. */
  readonly discovery: FleetDiscoveryStatus;
  readonly runs: number;
  /**
   * How many of the host's runs its last snapshot did not list, because listing them would pass its
   * byte budget (the oldest finished ones); zero when it listed every run.
   */
  readonly runsLeftOut: number;
}

export interface FleetView {
  /** Live runs, newest start first. */
  readonly live: readonly FleetRow[];
  /** Runs that are over, newest end first. */
  readonly history: readonly FleetRow[];
  /** Runs whose phase-one entry cannot truthfully be called live or ended. */
  readonly unknown: readonly FleetRow[];
  /** Every host, in the order given, whether or not it has a row. */
  readonly hosts: readonly FleetHostView[];
  /** Whether an empty filtered union is proven, or why it is not. */
  readonly absence: FleetAbsence;
}

export type FleetAbsence =
  | {readonly kind: 'complete'}
  | {readonly kind: 'opening'}
  | {readonly kind: 'unconfirmed'}
  | {readonly kind: 'unavailable'; readonly reason: GenericRunIndexUnavailable | null};

const LIVE_STATUSES: ReadonlySet<RunStatus> = new Set([
  RunStatus.STARTING,
  RunStatus.RUNNING,
  RunStatus.STOPPING,
]);

/** Whether a run in `status` is still working. */
export const isLive = (status: RunStatus): boolean => LIVE_STATUSES.has(status);

const LIMIT: Readonly<Record<RunLimit, 'time' | 'cost' | null>> = {
  [RunLimit.UNSPECIFIED]: null,
  [RunLimit.TIME]: 'time',
  [RunLimit.COST]: 'cost',
};

/** Why the run wants a look, the most pressing first: pushed in the order of `ATTENTION_KINDS`. */
function attentionOf(run: Run, search: SearchState | undefined): readonly FleetAttention[] {
  if (!isLive(run.status)) {
    const checkouts = run.keptCheckouts.length;
    return checkouts === 0 ? [] : [{kind: 'kept', checkouts}];
  }
  const found: FleetAttention[] = [];
  const stalled = ms(run.stalledSince);
  if (stalled !== null) found.push({kind: 'stalled', sinceMs: stalled});
  const jobs = search?.troubles.length ?? 0;
  if (jobs > 0) found.push({kind: 'troubles', jobs});
  const limit = LIMIT[run.limitReached];
  if (limit !== null) found.push({kind: 'limit', limit, atMs: ms(run.limitReachedAt)});
  if (run.finalReported) found.push({kind: 'final'});
  return found;
}

function rowOf(
  host: FleetHostRef,
  summary: RunSummary | undefined,
  search: SearchState | undefined,
  run: Run,
): ManagedFleetRow {
  const live = isLive(run.status);
  return {
    source: 'managed',
    key: fleetKey(host.key, run.id),
    host,
    runId: run.id,
    name: run.task,
    card: run.taskCard ?? null,
    repository: run.repository,
    setup: inSetup(run),
    status: run.status,
    live,
    section: live ? 'live' : 'history',
    startedMs: ms(run.createdAt),
    endedMs: ms(run.endedAt),
    objectives: summary?.objectives.map(objectiveOf) ?? [],
    costUsd: run.usage?.costUsd ?? null,
    budgetUsd: run.maxCostUsd ?? null,
    agentsRunning: summary?.agentsWorking ?? null,
    lastReportMs: ms(summary?.report?.at),
    reportUnavailable: summary?.reportUnavailable ?? false,
    deadlineMs: ms(run.deadline),
    attention: attentionOf(run, search),
  };
}

function genericRowOf(
  host: FleetHostRef,
  run: GenericRunEntry,
  freshness: 'current' | 'stale',
): GenericFleetRow {
  // A generic row's host status belongs to the generic index. The managed WatchRuns feed may be
  // failed at the same time and must not make a current generic row say its own discovery failed.
  const genericHost: FleetHostRef = {
    ...host,
    status:
      freshness === 'current'
        ? {kind: 'live', cause: null, staleForMs: null}
        : {kind: 'stale', cause: null, staleForMs: null},
  };
  return {
    source: 'generic',
    key: fleetKey(host.key, run.runId),
    host: genericHost,
    runId: run.runId,
    name: 'Run',
    card: null,
    repository: run.projectRoot ?? '',
    state: run.state,
    projectRoot: run.projectRoot,
    indexFreshness: freshness,
    needsUser: run.needsUser,
    live: run.activity === 'live',
    section: run.activity === 'live' ? 'live' : run.activity === 'ended' ? 'history' : 'unknown',
    startedMs: null,
    endedMs: null,
    objectives:
      run.objective === null
        ? []
        : [
            {
              name: run.objective.name,
              value: run.objective.bestValue,
              unit: run.objective.unit,
              percentOfBound: null,
            },
          ],
    costUsd: run.costUsd,
    budgetUsd: null,
    agentsRunning: null,
    lastReportMs: null,
    reportUnavailable: false,
    deadlineMs: null,
    attention: [],
  };
}

/**
 * Every run of every host as a row. A run that failed before its lead ever ran is left out, as the
 * host's own listing leaves it out: it is a refused start more than a run.
 */
export function fleetRows(hosts: readonly FleetHostInput[]): readonly FleetRow[] {
  return hosts.flatMap(host => {
    const ref: FleetHostRef = {
      key: host.key,
      label: host.label,
      freshness: host.freshness,
      status: host.status,
    };
    const managed = Object.values(host.data.runs)
      .filter(run => !run.neverOpened)
      .map(run => rowOf(ref, host.data.summaries[run.id], host.data.searches[run.id], run));
    const {runIndex} = host;
    if (runIndex._tag !== 'Ready') return managed;
    const managedIds = new Set(managed.map(row => row.runId));
    const generic = [...runIndex.runs.values()]
      .filter(run => !managedIds.has(run.runId))
      .map(run => genericRowOf(ref, run, runIndex.freshness));
    return [...managed, ...generic];
  });
}

const byKey = (a: FleetRow, b: FleetRow): number => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);

/** Newest first; a row with no time of its own goes last; ties fall to the key so the order is total. */
function newestFirst(time: (row: FleetRow) => number | null) {
  return (a: FleetRow, b: FleetRow): number =>
    (time(b) ?? Number.NEGATIVE_INFINITY) - (time(a) ?? Number.NEGATIVE_INFINITY) || byKey(a, b);
}

const startedFirst = newestFirst(row => row.startedMs);
const endedFirst = newestFirst(row => row.endedMs ?? row.startedMs);

const inState = (state: FleetState, row: FleetRow): boolean =>
  state === 'all' || row.section === (state === 'live' ? 'live' : 'history');

const absenceRank: Readonly<Record<FleetAbsence['kind'], number>> = {
  complete: 0,
  opening: 1,
  unconfirmed: 2,
  unavailable: 3,
};

/** The less conclusive evidence; on a tie, retain an unavailable stream's typed reason. */
function worseAbsence(left: FleetAbsence, right: FleetAbsence): FleetAbsence {
  const difference = absenceRank[left.kind] - absenceRank[right.kind];
  if (difference !== 0) return difference > 0 ? left : right;
  if (left.kind !== 'unavailable' || right.kind !== 'unavailable') return left;
  return left.reason === null && right.reason !== null ? right : left;
}

/** What one host can prove about the absence of runs in its two disjoint discovery streams. */
function hostAbsence(host: FleetHostInput): FleetAbsence {
  const managed: FleetAbsence =
    host.freshness === 'live'
      ? {kind: 'complete'}
      : host.freshness === 'empty'
        ? {kind: 'opening'}
        : host.freshness === 'stale'
          ? {kind: 'unconfirmed'}
          : {kind: 'unavailable', reason: null};
  const generic: FleetAbsence = (() => {
    switch (host.runIndex._tag) {
      case 'Dormant':
      case 'Loading':
        return {kind: 'opening'};
      case 'Ready':
        return host.runIndex.freshness === 'current' ? {kind: 'complete'} : {kind: 'unconfirmed'};
      case 'Unavailable':
        return {kind: 'unavailable', reason: host.runIndex.reason};
    }
  })();
  return worseAbsence(managed, generic);
}

function combinedAbsence(hosts: readonly FleetHostInput[]): FleetAbsence {
  return hosts.map(hostAbsence).reduce<FleetAbsence>(worseAbsence, {kind: 'complete'});
}

const genericDiscoveryStatus = (index: GenericRunIndex): GenericDiscoveryStatus => {
  switch (index._tag) {
    case 'Dormant':
    case 'Loading':
      return {kind: 'opening'};
    case 'Ready':
      return {kind: index.freshness};
    case 'Unavailable':
      return {kind: 'unavailable', reason: index.reason};
  }
};

/**
 * The fleet under `filter`: live runs, then history, and every host with the number of runs it
 * would show under the state filter. A host with no data yet, or one that cannot be reached, still
 * appears in `hosts` (its freshness says so), and one whose link is down keeps the rows it had.
 */
export function fleetView(hosts: readonly FleetHostInput[], filter: FleetFilter): FleetView {
  const includedHosts = hosts.filter(host => filter.host === null || host.key === filter.host);
  const allRows = fleetRows(hosts);
  const includedRows = allRows.filter(row => filter.host === null || row.host.key === filter.host);
  const byState = allRows.filter(row => inState(filter.state, row));
  const shown = byState.filter(row => filter.host === null || row.host.key === filter.host);
  const statusUncertain =
    filter.state !== 'all' && includedRows.some(row => row.section === 'unknown');
  const evidence = combinedAbsence(includedHosts);
  return {
    live: shown.filter(row => row.section === 'live').sort(startedFirst),
    history: shown.filter(row => row.section === 'history').sort(endedFirst),
    unknown: shown.filter(row => row.section === 'unknown').sort(byKey),
    hosts: hosts.map(host => ({
      key: host.key,
      label: host.label,
      discovery: {
        managed: {freshness: host.freshness, status: host.status},
        generic: genericDiscoveryStatus(host.runIndex),
      },
      runs: byState.filter(row => row.host.key === host.key).length,
      runsLeftOut: host.data.cut.runsLeftOut,
    })),
    absence: evidence.kind === 'complete' && statusUncertain ? {kind: 'unconfirmed'} : evidence,
  };
}

/**
 * The objectives of a run that have a measurement, each with every candidate and the points the
 * host marked as setting a best: what a row draws of the run's history. Nothing is worked out here;
 * a host that serves no series (an older host) gives none, and the row draws nothing for it.
 */
export function measuredObjectives(
  search: SearchState | undefined,
  standings: readonly ObjectiveStanding[],
): readonly ObjectiveReport[] {
  return search === undefined
    ? []
    : objectiveReports(search, standings).filter(objective => objective.points.length > 0);
}

/**
 * How to read the measured objectives of a row from the hosts the fleet was made of. The reading is
 * done per row asked for, so the rows a list does not draw cost nothing.
 */
export function rowObjectives(
  hosts: readonly FleetHostInput[],
): (row: FleetRow) => readonly ObjectiveReport[] {
  const byHost = new Map(hosts.map(host => [host.key, host.data] as const));
  return row => {
    const data = byHost.get(row.host.key);
    return measuredObjectives(
      data?.searches[row.runId],
      data?.summaries[row.runId]?.objectives ?? [],
    );
  };
}
