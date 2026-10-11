/**
 * The Recent list's store: the runs this user opened (the main process's settings store is their
 * one writer and pushes every change, `recent`), and each run's status, asked of its host.
 *
 * One status request per host is awaited at a time; a newer one replaces it and a reply with
 * another token is dropped. Statuses already known stay shown while they are asked again. They are
 * asked again whenever the list changes, the window comes back from a run, a stop changes, or a stop
 * is ending (`tick`).
 */
import type {HostKey} from '../../host-settings.js';
import type {
  ChromeState,
  WelcomeRecent,
  WelcomeRecentStatus,
  WelcomeResult,
} from '../../welcome-protocol.js';
import {only, type Step} from './step.js';

interface HostStatuses {
  /** The status request awaited, or null. */
  readonly awaiting: number | null;
  /** Statuses by instance id, from the last reply; null before one arrived. */
  readonly known: Readonly<Record<string, WelcomeRecentStatus>> | null;
  /** Why the last reply failed, or null. */
  readonly error: string | null;
}

export interface RecentState {
  readonly next: number;
  readonly runs: readonly WelcomeRecent[];
  readonly hosts: Readonly<Record<HostKey, HostStatuses>>;
  readonly mode: ChromeState['mode'];
  /** The stop flows, serialized, to notice a change. */
  readonly stops: string;
}

export type RecentAction =
  | {readonly type: 'runs'; readonly runs: readonly WelcomeRecent[]}
  | {readonly type: 'chrome'; readonly chrome: ChromeState}
  | {readonly type: 'tick'}
  | {
      readonly type: 'status-reply';
      readonly host: HostKey;
      readonly token: number;
      readonly result: WelcomeResult<Readonly<Record<string, WelcomeRecentStatus>>>;
    };

export type RecentRequest = {
  readonly kind: 'status';
  readonly token: number;
  readonly host: HostKey;
};

export const RECENT_EMPTY: RecentState = {
  next: 1,
  runs: [],
  hosts: {},
  mode: 'welcome',
  stops: '{}',
};

/** A run's status as the list shows it: the last known, the host's failure, or not yet known. */
export function statusOf(state: RecentState, run: WelcomeRecent): WelcomeRecentStatus | undefined {
  const host = state.hosts[run.host];
  if (host === undefined) return undefined;
  if (host.error !== null) return {kind: 'unknown', detail: host.error};
  return host.known?.[run.instanceId];
}

export function recentInvariants(state: RecentState): string[] {
  const problems: string[] = [];
  for (const [host, statuses] of Object.entries(state.hosts)) {
    if (!state.runs.some(run => run.host === host)) problems.push(`statuses for unlisted ${host}`);
    if (statuses.awaiting !== null && statuses.awaiting >= state.next) {
      problems.push(`token ${statuses.awaiting} not issued`);
    }
  }
  return problems;
}

/** Ask every listed host for its runs' statuses again, keeping what is known meanwhile. */
function refresh(state: RecentState): Step<RecentState, RecentRequest> {
  let next = state.next;
  const hosts: Record<HostKey, HostStatuses> = {};
  const requests: RecentRequest[] = [];
  for (const host of new Set(state.runs.map(run => run.host))) {
    const token = next;
    next += 1;
    hosts[host] = {awaiting: token, known: state.hosts[host]?.known ?? null, error: null};
    requests.push({kind: 'status', token, host});
  }
  return {state: {...state, next, hosts}, requests};
}

export function recentStep(
  state: RecentState,
  action: RecentAction,
): Step<RecentState, RecentRequest> {
  switch (action.type) {
    case 'runs':
      return refresh({...state, runs: action.runs});
    case 'tick':
      return refresh(state);
    case 'chrome': {
      const stops = JSON.stringify(action.chrome.stops);
      const returned = state.mode === 'run' && action.chrome.mode === 'welcome';
      const next = {...state, mode: action.chrome.mode, stops};
      return returned || stops !== state.stops ? refresh(next) : only(next);
    }
    case 'status-reply': {
      const host = state.hosts[action.host];
      if (host === undefined || host.awaiting !== action.token) return only(state);
      const result = action.result;
      const statuses: HostStatuses = result.ok
        ? {awaiting: null, known: result.value, error: null}
        : {awaiting: null, known: host.known, error: result.error};
      return only({...state, hosts: {...state.hosts, [action.host]: statuses}});
    }
  }
}
