/**
 * The fleet: every run of every connected host as one row each, live runs first and then history,
 * with the filters of the Runs screen applied. A plain fold over what the hosts have said; it reads
 * no clock and starts no work, and it derives nothing the host serves: a percent of a bound, a cost
 * and a time are shown as sent, and absent exactly when the host sent none. The screen formats.
 */
import {
  AgentStatus,
  type Message,
  oneofIs,
  type Run,
  RunStatus,
  type SearchState,
} from '@vibesys/orchestrator-proto';
import type {Freshness} from '../freshness.js';
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
  /** Agents that are starting, working or waiting on a subagent. */
  readonly agentsRunning: number;
  /** When the lead last posted a report, or null when it has not. */
  readonly lastReportMs: number | null;
}

/** A host as the filter strip shows it, with its run count under the state filter. */
export interface FleetHostView extends FleetHostRef {
  readonly runs: number;
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

const WORKING_AGENTS: ReadonlySet<AgentStatus> = new Set([
  AgentStatus.LAUNCHING,
  AgentStatus.ACTIVE,
  AgentStatus.WAITING,
]);

/** Whether a run in `status` is still working. */
export const isLive = (status: RunStatus): boolean => LIVE_STATUSES.has(status);

const isReport = (message: Message): boolean =>
  oneofIs(message.body, 'report', {
    text: true,
    handoff: true,
    report: true,
    jobFinished: true,
    subagentSettled: true,
    brief: true,
    leadBrief: true,
    handoffReminder: true,
    leadIdleReminder: true,
    reportDue: true,
    catchUpRequest: true,
    runLimitReached: true,
  });

/** The head's value on each objective and the bounds served for them, merged by objective name. */
export function headObjectives(search: SearchState | undefined): readonly FleetObjective[] {
  if (search === undefined) return [];
  const head = search.head?.state;
  const outcome = oneofIs(head, 'current', {current: true, none: true})
    ? head.value.result?.outcome
    : undefined;
  const measure = oneofIs(outcome, 'measure', {check: true, measure: true})
    ? outcome.value
    : undefined;
  const bounds = new Map(search.bounds.map(entry => [entry.objective, entry] as const));
  const measured: {name: string; value: number | null}[] = [];
  if (measure !== undefined) {
    const scored = measure.metric?.name ?? '';
    const names = measure.objectives?.metrics.map(metric => metric.name) ?? [scored];
    for (const name of names) {
      const observed = measure.observations.find(each => each.name === name)?.value;
      measured.push({name, value: name === scored ? measure.value : (observed ?? null)});
    }
  }
  const named = new Set(measured.map(each => each.name));
  const rows = [
    ...measured,
    ...search.bounds
      .filter(entry => !named.has(entry.objective))
      .map(entry => ({name: entry.objective, value: null})),
  ];
  return rows.map(({name, value}) => {
    const bound = bounds.get(name);
    return {
      name,
      value: bound?.headValue ?? value,
      unit: bound?.versions.at(-1)?.unit ?? '',
      percentOfBound: bound?.percentOfBound ?? null,
    };
  });
}

interface RunFacts {
  readonly agentsRunning: Map<string, number>;
  readonly lastReportMs: Map<string, number>;
}

/** One pass over the agents and messages, so a row costs nothing per run to read them. */
function factsOf(data: OrchestratorState): RunFacts {
  const agentsRunning = new Map<string, number>();
  for (const agent of Object.values(data.agents)) {
    if (WORKING_AGENTS.has(agent.status))
      agentsRunning.set(agent.runId, (agentsRunning.get(agent.runId) ?? 0) + 1);
  }
  const lastReportMs = new Map<string, number>();
  for (const message of Object.values(data.messages)) {
    const at = ms(message.at);
    if (at === null || !isReport(message)) continue;
    lastReportMs.set(message.runId, Math.max(lastReportMs.get(message.runId) ?? at, at));
  }
  return {agentsRunning, lastReportMs};
}

function rowOf(host: FleetHostRef, data: OrchestratorState, facts: RunFacts, run: Run): FleetRow {
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
    objectives: headObjectives(data.searches[run.id]),
    costUsd: run.usage?.costUsd ?? null,
    budgetUsd: run.maxCostUsd ?? null,
    agentsRunning: facts.agentsRunning.get(run.id) ?? 0,
    lastReportMs: facts.lastReportMs.get(run.id) ?? null,
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
    const facts = factsOf(host.data);
    return Object.values(host.data.runs)
      .filter(run => !run.neverOpened)
      .map(run => rowOf(ref, host.data, facts, run));
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
    })),
  };
}

/**
 * The objectives of a run that have a measurement, each with every candidate and the points the
 * host marked as setting a best: what a row draws of the run's history. Nothing is worked out here;
 * a host that serves no series (an older host) gives none, and the row draws nothing for it.
 */
export function measuredObjectives(search: SearchState | undefined): readonly ObjectiveReport[] {
  return search === undefined
    ? []
    : objectiveReports(search).filter(objective => objective.points.length > 0);
}

/**
 * How to read the measured objectives of a row from the hosts the fleet was made of. The reading is
 * done per row asked for, so the rows a list does not draw cost nothing.
 */
export function rowObjectives(
  hosts: readonly FleetHostInput[],
): (row: FleetRow) => readonly ObjectiveReport[] {
  const byHost = new Map(hosts.map(host => [host.key, host.data] as const));
  return row => measuredObjectives(byHost.get(row.host.key)?.searches[row.runId]);
}
