/**
 * The sidebar's store: a pure reducer over what the hosts' watches push (their listings), the runs
 * this user opened (Recent), the runs archived, and which screen is shown. The rows the sidebar
 * draws are selectors over it, so a push is folded once and every row follows.
 */
import {type HostKey, type HostListing, type HostRun, hostFromKey, hostLabel} from '@vibesys/hosts';
import {type RecentRun, type RunRef, sameRun} from '@vibesys/runs';
import {endedStatus, liveDetail, liveStatus, type RunRowStatus, statusWord} from './run-status.js';

/** The screen the main area shows. */
export type Screen =
  | {readonly kind: 'new-run'}
  | {readonly kind: 'machines'}
  | {readonly kind: 'settings'}
  | {readonly kind: 'all-runs'}
  | {readonly kind: 'run'; readonly run: RunRef};

export interface RunRow {
  readonly run: RunRef;
  readonly title: string;
  readonly detail: string;
  readonly status: RunRowStatus;
}

/** What the sidebar's row menus ask of the app. */
export interface ShellActions {
  archive(run: RunRef): void;
}

export interface SidebarState {
  /** The latest listing of each host, one per host. */
  readonly listings: readonly HostListing[];
  readonly recent: readonly RecentRun[];
  readonly archived: readonly RunRef[];
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
  | {readonly type: 'select'; readonly screen: Screen};

export const SIDEBAR_EMPTY: SidebarState = {
  listings: [],
  recent: [],
  archived: [],
  selection: {kind: 'new-run'},
};

/** How many runs Recent shows before "Show all". */
export const RECENT_ROWS = 20;

function withListing(listings: readonly HostListing[], listing: HostListing): HostListing[] {
  return [...listings.filter(each => each.host !== listing.host), listing];
}

/** The next state for `action`; the sidebar makes no requests. */
export function sidebarStep(state: SidebarState, action: SidebarAction): SidebarState {
  switch (action.type) {
    case 'overview':
      return {
        ...state,
        listings: action.listings.reduce<HostListing[]>(withListing, []),
        recent: action.recent,
      };
    case 'listing':
      return {...state, listings: withListing(state.listings, action.listing)};
    case 'recent':
      return {...state, recent: action.recent};
    case 'archived':
      return {...state, archived: action.archived};
    case 'select':
      return {...state, selection: action.screen};
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
      const run: RunRef = {host: recent.host, runId: recent.runId};
      return {
        run,
        title: titleOf(state, run, recent.project),
        detail: `${label(recent.host)} · ${statusWord(status)}`,
        status,
      };
    });
  return {rows: rows.slice(0, RECENT_ROWS), more: Math.max(0, rows.length - RECENT_ROWS)};
}

/** The machines the sidebar counts: every listed host, and those whose watch cannot list now. */
export function machineCounts(state: SidebarState): {
  readonly count: number;
  readonly attention: number;
} {
  return {
    count: state.listings.length,
    attention: state.listings.filter(listing => listing.kind === 'unavailable').length,
  };
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
  return [
    ...(new Set(hosts).size === hosts.length ? [] : ['a host is listed twice']),
    ...recentProblems(state, live),
    ...liveProblems(live),
  ];
}
