/**
 * The fleet: every run of every connected host as one row each, live runs first and then history,
 * with the filters of the Runs screen applied. A plain fold over what the hosts have said; it reads
 * no clock and starts no work, and it derives nothing the host serves: the report a run is summed up
 * by, the agents working, the head of each objective, a percent of a bound, a cost and a time are
 * shown as sent (the run's `RunSummary`), and absent exactly when the host sent none. The screen
 * formats.
 */
import {
  type ObjectiveHead,
  type Run,
  RunStatus,
  type RunSummary,
} from '@vibesys/orchestrator-proto';
import type {Freshness} from '../freshness.js';
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

/** The key of the row of run `runId` on host `hostKey`: unique across hosts, and a route's own parts. */
export const fleetKey = (hostKey: string, runId: string): string => `${hostKey}/${runId}`;

/** One host the fleet reads, as the screen holds it. */
export interface FleetHostInput {
  /** The `HostKey`: what a route names. */
  readonly key: string;
  /** What the host is called on screen. */
  readonly label: string;
  readonly data: OrchestratorState;
  readonly freshness: Freshness;
  /** How long what is shown of the host has been out of date, when the service layer knows. */
  readonly staleForMs: number | null;
}

/** A host as a row names it. */
export interface FleetHostRef {
  readonly key: string;
  readonly label: string;
  readonly freshness: Freshness;
  readonly staleForMs: number | null;
}

/** The head's result on one objective, with the share of the declared bound when the host served one. */
export interface FleetObjective {
  readonly name: string;
  /** The head's value, or null when the head has none for this objective. */
  readonly value: number | null;
  /** The unit the agents declared the bound in; empty when no bound was declared. */
  readonly unit: string;
  /** How close the head is to the bound, in percent, as the host served it; null when it did not. */
  readonly percentOfBound: number | null;
}

/** The host's head of one objective as a row carries it: the same four facts, absent as null. */
const objectiveOf = (head: ObjectiveHead): FleetObjective => ({
  name: head.name,
  value: head.value ?? null,
  unit: head.unit,
  percentOfBound: head.percentOfBound ?? null,
});

export interface FleetRow {
  /** Unique across hosts: the host and the run, the same key a route is made from. */
  readonly key: string;
  readonly host: FleetHostRef;
  readonly runId: string;
  /** What the run is called: its task. */
  readonly name: string;
  readonly repository: string;
  readonly status: RunStatus;
  /** Still working: starting, running or stopping. */
  readonly live: boolean;
  readonly startedMs: number | null;
  /** When the run left RUNNING; null while it is live. */
  readonly endedMs: number | null;
  /** The head's result on each objective, in the order the host's measurement names them. */
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
}

/** A host as the filter strip shows it, with its run count under the state filter. */
export interface FleetHostView extends FleetHostRef {
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
  /** Every host, in the order given, whether or not it has a row. */
  readonly hosts: readonly FleetHostView[];
}

const LIVE_STATUSES: ReadonlySet<RunStatus> = new Set([
  RunStatus.STARTING,
  RunStatus.RUNNING,
  RunStatus.STOPPING,
]);

/** Whether a run in `status` is still working. */
export const isLive = (status: RunStatus): boolean => LIVE_STATUSES.has(status);

function rowOf(host: FleetHostRef, summary: RunSummary | undefined, run: Run): FleetRow {
  return {
    key: fleetKey(host.key, run.id),
    host,
    runId: run.id,
    name: run.task,
    repository: run.repository,
    status: run.status,
    live: isLive(run.status),
    startedMs: ms(run.createdAt),
    endedMs: ms(run.endedAt),
    objectives: summary?.objectives.map(objectiveOf) ?? [],
    costUsd: run.usage?.costUsd ?? null,
    budgetUsd: run.maxCostUsd ?? null,
    agentsRunning: summary?.agentsWorking ?? null,
    lastReportMs: ms(summary?.report?.at),
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
      staleForMs: host.staleForMs,
    };
    return Object.values(host.data.runs)
      .filter(run => !run.neverOpened)
      .map(run => rowOf(ref, host.data.summaries[run.id], run));
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
  state === 'all' || (state === 'live') === row.live;

/**
 * The fleet under `filter`: live runs, then history, and every host with the number of runs it
 * would show under the state filter. A host with no data yet, or one that cannot be reached, still
 * appears in `hosts` (its freshness says so), and one whose link is down keeps the rows it had.
 */
export function fleetView(hosts: readonly FleetHostInput[], filter: FleetFilter): FleetView {
  const byState = fleetRows(hosts).filter(row => inState(filter.state, row));
  const shown = byState.filter(row => filter.host === null || row.host.key === filter.host);
  return {
    live: shown.filter(row => row.live).sort(startedFirst),
    history: shown.filter(row => !row.live).sort(endedFirst),
    hosts: hosts.map(host => ({
      key: host.key,
      label: host.label,
      freshness: host.freshness,
      staleForMs: host.staleForMs,
      runs: byState.filter(row => row.host.key === host.key).length,
      runsLeftOut: host.data.cut.runsLeftOut,
    })),
  };
}
