/**
 * The sidebar's store: a pure reducer over what the hosts' watches push (their listings), the runs
 * this user opened (Recent), the runs archived, the saved machines and whether each is ready, and
 * which screen is shown. The rows the sidebar draws are selectors over it, so a push is folded once
 * and every row follows. The machines are the saved ones (`hosts.json`, the same list the Machines
 * screen reads), listed at start and again when the user leaves the Machines screen, where they
 * may have changed; each listing and check carries a token, so a late reply is dropped.
 */
import {type HostKey, type HostListing, type HostRun, hostFromKey, hostLabel} from '@vibesys/hosts';
import {type RecentRun, type RunRef, sameRun} from '@vibesys/runs';
import type {Step} from '@vibesys/store';
import {
  endedRowLook,
  endedStatus,
  liveDetail,
  liveLook,
  liveStatus,
  type RowLook,
  type RunRowStatus,
} from './run-status.js';

/** The screen the main area shows. */
export type Screen =
  /**
   * New run; opened for a machine (Machines' "New run here"), it starts on that machine; opened
   * to run `again` a run, it starts from that run's place and options.
   */
  | {readonly kind: 'new-run'; readonly machine?: HostKey; readonly again?: RunRef}
  | {readonly kind: 'machines'}
  | {readonly kind: 'settings'}
  | {readonly kind: 'all-runs'}
  | {readonly kind: 'run'; readonly run: RunRef};

export interface RunRow {
  readonly run: RunRef;
  readonly title: string;
  readonly detail: string;
  readonly status: RunRowStatus;
  /** The dot's tone and the row's status word. */
  readonly look: RowLook;
}

/** What the sidebar's row menus ask of the app. */
export interface ShellActions {
  archive(run: RunRef): void;
  /**
   * Show `run` in a window of its own, where it is then live; this window leaves its Run screen
   * for New run when it showed that run.
   */
  openWindow(run: RunRef): void;
}

/** One saved machine: whether its last check found it ready, or the check awaited. */
export interface SidebarMachine {
  readonly host: HostKey;
  /** Null until a check answers. */
  readonly ready: boolean | null;
  readonly checking: number | null;
}

export interface SidebarMachines {
  /** The listing awaited, or null. */
  readonly listing: number | null;
  readonly saved: readonly SidebarMachine[];
  /** The last token issued, so each is new. */
  readonly lastToken: number;
}

export interface SidebarState {
  /** The latest listing of each host, one per host. */
  readonly listings: readonly HostListing[];
  readonly recent: readonly RecentRun[];
  readonly archived: readonly RunRef[];
  readonly machines: SidebarMachines;
  readonly selection: Screen;
}

export type SidebarAction =
  | {
      readonly type: 'overview';
      readonly listings: readonly HostListing[];
      readonly recent: readonly RecentRun[];
    }
  | {readonly type: 'listing'; readonly listing: HostListing}
  | {readonly type: 'recent'; readonly recent: readonly RecentRun[]}
  | {readonly type: 'archived'; readonly archived: readonly RunRef[]}
  | {readonly type: 'select'; readonly screen: Screen}
  /** The saved machines, or why they could not be read. */
  | {
      readonly type: 'machines-listed';
      readonly token: number;
      readonly hosts: readonly HostKey[] | null;
    }
  /** Whether one machine's check found it ready (`machine` answered `ready`). */
  | {
      readonly type: 'machine-checked';
      readonly token: number;
      readonly host: HostKey;
      readonly ready: boolean;
    };

/** What the sidebar asks of the main process: the saved machines, and one machine's check. */
export type SidebarRequest =
  | {readonly kind: 'list-machines'; readonly token: number}
  | {readonly kind: 'check-machine'; readonly token: number; readonly host: HostKey};

export type SidebarStep = Step<SidebarState, SidebarRequest>;

export const SIDEBAR_EMPTY: SidebarState = {
  listings: [],
  recent: [],
  archived: [],
  machines: {listing: null, saved: [], lastToken: 0},
  selection: {kind: 'new-run'},
};

function listMachines(state: SidebarState): SidebarStep {
  const token = state.machines.lastToken + 1;
  return {
    state: {...state, machines: {...state.machines, listing: token, lastToken: token}},
    requests: [{kind: 'list-machines', token}],
  };
}

/** The sidebar before anything is heard: it asks for the saved machines. */
export function initialSidebar(): SidebarStep {
  return listMachines(SIDEBAR_EMPTY);
}

function same(state: SidebarState): SidebarStep {
  return {state, requests: []};
}

/** The listed machines, each checked again; what was known of one that stays is kept meanwhile. */
function machinesListed(
  state: SidebarState,
  action: Extract<SidebarAction, {type: 'machines-listed'}>,
): SidebarStep {
  const {machines} = state;
  if (machines.listing !== action.token) return same(state);
  if (action.hosts === null) return same({...state, machines: {...machines, listing: null}});
  let last = machines.lastToken;
  const requests: SidebarRequest[] = [];
  const saved = [...new Set(action.hosts)].map(host => {
    last += 1;
    requests.push({kind: 'check-machine', token: last, host});
    const known = machines.saved.find(machine => machine.host === host);
    return {host, ready: known?.ready ?? null, checking: last};
  });
  return {state: {...state, machines: {listing: null, saved, lastToken: last}}, requests};
}

function machineChecked(
  state: SidebarState,
  action: Extract<SidebarAction, {type: 'machine-checked'}>,
): SidebarStep {
  const machine = state.machines.saved.find(each => each.host === action.host);
  if (machine === undefined || machine.checking !== action.token) return same(state);
  const saved = state.machines.saved.map(each =>
    each === machine ? {...each, ready: action.ready, checking: null} : each,
  );
  return same({...state, machines: {...state.machines, saved}});
}

/** The selection, and a fresh listing when the user leaves the Machines screen. */
function selected(state: SidebarState, screen: Screen): SidebarStep {
  const next = {...state, selection: screen};
  const leaving = state.selection.kind === 'machines' && screen.kind !== 'machines';
  return leaving ? listMachines(next) : same(next);
}

/** How many runs Recent shows before "Show all". */
export const RECENT_ROWS = 20;

function withListing(listings: readonly HostListing[], listing: HostListing): HostListing[] {
  return [...listings.filter(each => each.host !== listing.host), listing];
}

/** The next state for `action`, and the requests it makes. */
export function sidebarStep(state: SidebarState, action: SidebarAction): SidebarStep {
  switch (action.type) {
    case 'overview':
      return same({
        ...state,
        listings: action.listings.reduce<HostListing[]>(withListing, []),
        recent: action.recent,
      });
    case 'listing':
      return same({...state, listings: withListing(state.listings, action.listing)});
    case 'recent':
      return same({...state, recent: action.recent});
    case 'archived':
      return same({...state, archived: action.archived});
    case 'select':
      return selected(state, action.screen);
    case 'machines-listed':
      return machinesListed(state, action);
    case 'machine-checked':
      return machineChecked(state, action);
  }
}

function projectName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

function label(host: HostKey): string {
  return hostLabel(hostFromKey(host));
}

/** The newest record of each run in `runs`, by run id. */
function newestByRun(runs: readonly HostRun[]): Map<string, HostRun> {
  const newest = new Map<string, HostRun>();
  for (const run of runs) {
    const known = newest.get(run.runId);
    if (known === undefined || known.startedAt < run.startedAt) newest.set(run.runId, run);
  }
  return newest;
}

function titleOf(state: SidebarState, run: RunRef, project: string): string {
  const task = state.recent.find(recent => sameRun(recent, run))?.task ?? null;
  const name = projectName(project);
  return task === null ? name : `${name} · ${task}`;
}

/** The runs a live server drives now, on every listed host. */
export function liveRows(state: SidebarState): RunRow[] {
  return state.listings.flatMap(listing => {
    if (listing.kind !== 'listed') return [];
    return [...newestByRun(listing.live).values()].map(server => {
      const run: RunRef = {host: listing.host, runId: server.runId};
      return {
        run,
        title: titleOf(state, run, server.projectRoot),
        detail: `${label(listing.host)} · ${liveDetail(server)}`,
        status: liveStatus(server),
        look: liveLook(server),
      };
    });
  });
}

/**
 * Recent: the runs this user opened that no server drives now and that are not archived, newest
 * first, at most `RECENT_ROWS`; `more` counts the rest.
 */
export function recentRows(state: SidebarState): {readonly rows: RunRow[]; readonly more: number} {
  const live = liveRows(state);
  const rows = state.recent
    .filter(recent => !state.archived.some(archived => sameRun(archived, recent)))
    .filter(recent => !live.some(row => sameRun(row.run, recent)))
    .sort((left, right) => right.attachedAt - left.attachedAt)
    .map(recent => {
      const listing = state.listings.find(each => each.host === recent.host);
      const ended =
        listing?.kind === 'listed' ? newestByRun(listing.ended).get(recent.runId) : undefined;
      const status = endedStatus(ended);
      const look = endedRowLook(ended);
      const run: RunRef = {host: recent.host, runId: recent.runId};
      return {
        run,
        title: titleOf(state, run, recent.project),
        detail: `${label(recent.host)} · ${look.word}`,
        status,
        look,
      };
    });
  return {rows: rows.slice(0, RECENT_ROWS), more: Math.max(0, rows.length - RECENT_ROWS)};
}

/**
 * The machines the sidebar counts: every saved machine, and those whose last check found them not
 * ready (checkout not found, sign-in, or unreachable).
 */
export function machineCounts(state: SidebarState): {
  readonly count: number;
  readonly attention: number;
} {
  const {saved} = state.machines;
  return {count: saved.length, attention: saved.filter(machine => machine.ready === false).length};
}

function recentProblems(state: SidebarState, live: readonly RunRow[]): string[] {
  const problems: string[] = [];
  const {rows} = recentRows(state);
  if (rows.length > RECENT_ROWS) problems.push('Recent shows more than its limit');
  for (const row of rows) {
    if (state.archived.some(archived => sameRun(archived, row.run))) {
      problems.push(`archived ${row.run.runId} is in Recent`);
    }
    if (live.some(each => sameRun(each.run, row.run))) {
      problems.push(`${row.run.runId} is both live and recent`);
    }
    if (row.status === 'live' || row.status === 'paused') {
      problems.push(`recent ${row.run.runId} reads as live`);
    }
  }
  return problems;
}

function liveProblems(live: readonly RunRow[]): string[] {
  const problems: string[] = [];
  for (const row of live) {
    if (row.status !== 'live' && row.status !== 'paused') {
      problems.push(`live ${row.run.runId} reads as ended`);
    }
    if (live.filter(each => sameRun(each.run, row.run)).length > 1) {
      problems.push(`${row.run.runId} is live twice`);
    }
  }
  return problems;
}

/** What must hold in every state; empty when all do. */
export function sidebarInvariants(state: SidebarState): string[] {
  const hosts = state.listings.map(listing => listing.host);
  const live = liveRows(state);
  const machines = state.machines.saved.map(machine => machine.host);
  const {attention, count} = machineCounts(state);
  return [
    ...(new Set(hosts).size === hosts.length ? [] : ['a host is listed twice']),
    ...(new Set(machines).size === machines.length ? [] : ['a machine is counted twice']),
    ...(attention <= count ? [] : ['more machines need attention than are saved']),
    ...recentProblems(state, live),
    ...liveProblems(live),
  ];
}
