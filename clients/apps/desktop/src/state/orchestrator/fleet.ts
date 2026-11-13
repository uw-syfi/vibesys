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
  Harness,
  type ObjectiveStanding,
  type Run,
  RunLimit,
  type RunSearchResult,
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
import {messageIs} from './messages.js';
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
  /** Plain operator words matched against the run's semantic search document. */
  readonly query?: string;
}

export const NO_FLEET_FILTER: FleetFilter = {state: 'all', host: null, query: ''};

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
  readonly runIndex?: GenericRunIndex;
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

/**
 * The newest structured account of work on a run. A hypothesis is a plan the host explicitly says
 * belongs to this run; a workstream is one this run started. A report is the honest fallback when
 * neither exists. The list never extracts an update from narration.
 */
export type FleetUpdate =
  | {
      readonly kind: 'hypothesis' | 'workstream' | 'report';
      readonly title: string;
      readonly detail: string;
      readonly atMs: number | null;
    }
  | {readonly kind: 'none'};

/** The semantic places an operator's words can match, with stable names for tests and diagnostics. */
export type FleetSearchField =
  | 'title'
  | 'description'
  | 'model'
  | 'cli'
  | 'work'
  | 'objective'
  | 'repository'
  | 'host'
  | 'run-id';

export interface FleetSearchEntry {
  readonly field: FleetSearchField;
  readonly value: string;
}

/**
 * One registry entry per searchable meaning. The weights are relevance, not presentation order:
 * a task title beats a path fragment, while every query token still has to match somewhere.
 */
export const FLEET_SEARCH_REGISTRY: Readonly<Record<FleetSearchField, {readonly weight: number}>> =
  {
    title: {weight: 12},
    description: {weight: 8},
    model: {weight: 11},
    cli: {weight: 9},
    work: {weight: 10},
    objective: {weight: 7},
    repository: {weight: 5},
    host: {weight: 4},
    'run-id': {weight: 3},
  };

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
  /** A short host-authored description, absent on rows made by pre-card consumers. */
  readonly description?: string;
  /** The newest structured work item or report the host served. */
  readonly update?: FleetUpdate;
  /** Changes when an operator-relevant structured update changes. */
  readonly activityKey?: string;
  /** The values searched by `fleetView`, absent only on rows made by pre-search consumers. */
  readonly search?: readonly FleetSearchEntry[];
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
  /** The run came from whole-history search and is absent from the bounded runs snapshot. */
  readonly searchOnly?: boolean;
}

/** One host's authoritative whole-history matches for exactly `query`. */
export interface FleetRemoteSearch {
  readonly host: string;
  readonly query: string;
  readonly results: readonly RunSearchResult[];
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

const HARNESS_SEARCH: Readonly<Record<Harness, string>> = {
  [Harness.UNSPECIFIED]: '',
  [Harness.CLAUDE_CODE]: 'Claude Code Claude',
  [Harness.CODEX]: 'Codex OpenAI',
};

/** A one-line description the task authored, without repeating its title. */
function descriptionOf(card: TaskCard | undefined): string {
  if (card === undefined) return '';
  return card.workloadDescription || card.heading;
}

const line = (value: string): string => value.split(/\r?\n/u)[0]?.trim() ?? '';

/** The newest plan explicitly in this run, the newest workstream it started, or its latest report. */
function updateOf(
  run: Run,
  summary: RunSummary | undefined,
  search: SearchState | undefined,
): FleetUpdate {
  const hypotheses = (search?.plans ?? []).flatMap((plan, index) => {
    if (search?.ideas[index]?.inRun !== true) return [];
    return [
      {
        kind: 'hypothesis' as const,
        title: plan.title,
        detail: line(plan.body),
        atMs: ms(plan.updatedAt),
        tie: plan.number,
      },
    ];
  });
  const workstreams = (search?.workstreams ?? []).flatMap(workstream =>
    workstream.origin?.runId !== run.id
      ? []
      : [
          {
            kind: 'workstream' as const,
            title: workstream.title,
            detail: workstream.note,
            atMs: ms(workstream.updatedAt),
            tie: workstream.number,
          },
        ],
  );
  const latest = [...hypotheses, ...workstreams].sort(
    (a, b) =>
      (b.atMs ?? Number.NEGATIVE_INFINITY) - (a.atMs ?? Number.NEGATIVE_INFINITY) || b.tie - a.tie,
  )[0];
  if (latest !== undefined) {
    return {
      kind: latest.kind,
      title: latest.title,
      detail: latest.detail,
      atMs: latest.atMs,
    };
  }
  const report = messageIs(summary?.report, 'report') ? summary.report.body.value : undefined;
  return report === undefined
    ? {kind: 'none'}
    : {
        kind: 'report',
        title: report.headline,
        detail: line(report.inFlight || report.changed),
        atMs: ms(summary?.report?.at),
      };
}

function searchEntries(
  host: FleetHostRef,
  run: Run,
  summary: RunSummary | undefined,
  search: SearchState | undefined,
  description: string,
): readonly FleetSearchEntry[] {
  const card = run.taskCard;
  const report = messageIs(summary?.report, 'report') ? summary.report.body.value : undefined;
  const entries: FleetSearchEntry[] = [
    {field: 'title', value: run.task},
    {field: 'title', value: card?.heading ?? ''},
    {field: 'description', value: description},
    {field: 'model', value: card?.modelName ?? ''},
    {field: 'description', value: card?.workloadName ?? ''},
    {field: 'description', value: card?.hardwareName ?? ''},
    {field: 'repository', value: run.repository},
    {field: 'host', value: `${host.label} ${host.key}`},
    {field: 'run-id', value: run.id},
    ...(card?.objectives ?? []).flatMap(objective => [
      {field: 'objective' as const, value: objective.name},
      {field: 'objective' as const, value: objective.unit},
    ]),
    ...run.allowedAgents.flatMap(agent => [
      {field: 'cli' as const, value: HARNESS_SEARCH[agent.harness]},
      {field: 'model' as const, value: agent.model},
    ]),
    ...(search?.workstreams ?? []).flatMap(workstream => [
      {field: 'work' as const, value: workstream.title},
      {field: 'work' as const, value: workstream.note},
    ]),
    ...(search?.plans ?? []).flatMap(plan => [
      {field: 'work' as const, value: plan.title},
      {field: 'work' as const, value: plan.body},
    ]),
    {field: 'work', value: report?.headline ?? ''},
    {field: 'work', value: report?.changed ?? ''},
    {field: 'work', value: report?.inFlight ?? ''},
    {field: 'work', value: report?.decisions ?? ''},
  ];
  return entries.filter(entry => entry.value.trim() !== '');
}

function rowOf(
  host: FleetHostRef,
  summary: RunSummary | undefined,
  search: SearchState | undefined,
  run: Run,
  searchOnly = false,
): ManagedFleetRow {
  const live = isLive(run.status);
  const description = descriptionOf(run.taskCard);
  const update = updateOf(run, summary, search);
  const reportSequence = messageIs(summary?.report, 'report')
    ? summary.report.body.value.sequence
    : 0;
  return {
    source: 'managed',
    key: fleetKey(host.key, run.id),
    host,
    runId: run.id,
    name: run.task,
    card: run.taskCard ?? null,
    description,
    update,
    activityKey: [
      String(run.status),
      String(reportSequence),
      update.kind,
      update.kind === 'none' ? '' : String(update.atMs ?? ''),
    ].join(':'),
    search: searchEntries(host, run, summary, search, description),
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
    searchOnly,
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
    description: '',
    update: {kind: 'none'},
    activityKey: `${run.state}:${run.needsUser}:${freshness}`,
    search: [
      {field: 'title', value: 'Run'},
      {field: 'description', value: run.state},
      {field: 'repository', value: run.projectRoot ?? ''},
      {field: 'host', value: `${host.label} ${host.key}`},
      {field: 'run-id', value: run.runId},
      {field: 'objective', value: run.objective?.name ?? ''},
      {field: 'objective', value: run.objective?.unit ?? ''},
    ].filter(entry => entry.value.trim() !== '') as readonly FleetSearchEntry[],
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
    searchOnly: false,
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
    const runIndex = host.runIndex ?? ({_tag: 'Dormant'} as const);
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
    const index = host.runIndex ?? ({_tag: 'Dormant'} as const);
    switch (index._tag) {
      case 'Dormant':
      case 'Loading':
        return {kind: 'opening'};
      case 'Ready':
        return index.freshness === 'current' ? {kind: 'complete'} : {kind: 'unconfirmed'};
      case 'Unavailable':
        return {kind: 'unavailable', reason: index.reason};
    }
  })();
  return worseAbsence(managed, generic);
}

function combinedAbsence(hosts: readonly FleetHostInput[]): FleetAbsence {
  return hosts.map(hostAbsence).reduce<FleetAbsence>(worseAbsence, {kind: 'complete'});
}

const genericDiscoveryStatus = (index: GenericRunIndex | undefined): GenericDiscoveryStatus => {
  if (index === undefined) return {kind: 'opening'};
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

const searchWords = (value: string | undefined): readonly string[] =>
  (value ?? '')
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^\p{Letter}\p{Number}]+/gu, ' ')
    .trim()
    .split(/\s+/u)
    .filter(Boolean);

function entryScore(entry: FleetSearchEntry, token: string): number {
  const words = searchWords(entry.value);
  if (words.length === 0) return 0;
  const weight = FLEET_SEARCH_REGISTRY[entry.field].weight;
  if (words.length === 1 && words[0] === token) return weight * 4;
  if (words.some(word => word === token)) return weight * 3;
  if (words.some(word => word.startsWith(token))) return weight * 2;
  return words.join(' ').includes(token) ? weight : 0;
}

/**
 * Relevance for an operator query, or null when any token has no semantic match. Every token may
 * match a different registered field (`codex cache`, for example); empty queries leave ordering to
 * the fleet's time sort.
 */
export function fleetSearchScore(row: FleetRow, query: string): number | null {
  const tokens = searchWords(query);
  if (tokens.length === 0) return 0;
  let total = 0;
  for (const token of tokens) {
    const best = Math.max(0, ...(row.search ?? []).map(entry => entryScore(entry, token)));
    if (best === 0) return null;
    total += best;
  }
  return total;
}

function searched(rows: readonly FleetRow[], query: string): readonly FleetRow[] {
  if (searchWords(query).length === 0) return rows;
  return rows
    .flatMap(row => {
      const score = fleetSearchScore(row, query);
      return score === null ? [] : [{row, score}];
    })
    .sort((a, b) => b.score - a.score || byKey(a.row, b.row))
    .map(found => found.row);
}

const sameQuery = (left: string | undefined, right: string | undefined): boolean =>
  searchWords(left).join('\u0000') === searchWords(right).join('\u0000');

/**
 * Rows the host found outside (or with facts beyond) the bounded snapshot. Search results are
 * trusted as matches: a plan or workstream that matched is intentionally not repeated in the
 * response. When the snapshot does hold the run, its SearchState can still supply the graph.
 */
function remoteRows(
  hosts: readonly FleetHostInput[],
  filter: FleetFilter,
  remote: readonly FleetRemoteSearch[],
): readonly FleetRow[] {
  if (searchWords(filter.query).length === 0) return [];
  return remote.flatMap(answer => {
    if (!sameQuery(answer.query, filter.query)) return [];
    if (filter.host !== null && answer.host !== filter.host) return [];
    const host = hosts.find(candidate => candidate.key === answer.host);
    if (host === undefined) return [];
    const ref: FleetHostRef = {
      key: host.key,
      label: host.label,
      freshness: host.freshness,
      status: host.status,
    };
    return answer.results.flatMap(result => {
      const {run, summary} = result;
      if (
        run === undefined ||
        summary === undefined ||
        run.neverOpened ||
        run.id === '' ||
        summary.runId !== run.id
      )
        return [];
      const row = rowOf(
        ref,
        summary,
        host.data.searches[run.id],
        run,
        host.data.runs[run.id] === undefined,
      );
      return inState(filter.state, row) ? [row] : [];
    });
  });
}

/**
 * The fleet under `filter`: live runs, then history, and every host with the number of runs it
 * would show under the state filter. A host with no data yet, or one that cannot be reached, still
 * appears in `hosts` (its freshness says so), and one whose link is down keeps the rows it had.
 */
export function fleetView(
  hosts: readonly FleetHostInput[],
  filter: FleetFilter,
  remote: readonly FleetRemoteSearch[] = [],
): FleetView {
  const includedHosts = hosts.filter(host => filter.host === null || host.key === filter.host);
  const allRows = fleetRows(hosts);
  const includedRows = allRows.filter(row => filter.host === null || row.host.key === filter.host);
  const byState = allRows.filter(row => inState(filter.state, row));
  const statusUncertain =
    filter.state !== 'all' && includedRows.some(row => row.section === 'unknown');
  const evidence = combinedAbsence(includedHosts);
  const local = searched(
    byState.filter(row => filter.host === null || row.host.key === filter.host),
    filter.query ?? '',
  );
  const seen = new Set(local.map(row => row.key));
  const shown = [
    ...local,
    ...remoteRows(hosts, filter, remote).filter(row => {
      if (seen.has(row.key)) return false;
      seen.add(row.key);
      return true;
    }),
  ];
  const ordered = searchWords(filter.query).length > 0;
  const section = (name: FleetRow['section'], order: (a: FleetRow, b: FleetRow) => number) => {
    const rows = shown.filter(row => row.section === name);
    return ordered ? rows : rows.sort(order);
  };
  return {
    live: section('live', startedFirst),
    history: section('history', endedFirst),
    unknown: section('unknown', byKey),
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
