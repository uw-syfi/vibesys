/**
 * The one status a host has on screen. A host's link, the feed read over it and whether anything
 * was read yet are not three facts a screen shows side by side: they are one, and this names it
 * once, worst first, so no two screens say two words for the same host.
 */
import {type HostConnectionState, refusalCauseOf} from './connection.js';
import type {RefusalCause} from './fault.js';
import type {FeedStatus} from './feed.js';

/**
 * Best to worst, which is also the order `hostStatusRank` gives:
 * - `live`: connected and the feed delivers.
 * - `catching-up`: connected, the feed is opening or restarting, earlier data is on screen.
 * - `waiting`: nothing read yet, and nothing wrong yet (not asked, or the feed is opening).
 * - `connecting`: the first dial is in flight, nothing read yet.
 * - `stale`: data is on screen that may be out of date (the link is down, or the feed failed).
 * - `unavailable`: the link is up but the feed failed, and nothing was ever read.
 * - `unreachable`: the host did not answer and will not be dialed again until the user retries.
 * - `incompatible`: the app and the host cannot talk.
 * - `refused`: the host cannot work for a reason no retry changes; `cause` says which when known.
 */
export type HostStatusKind =
  | 'live'
  | 'catching-up'
  | 'waiting'
  | 'connecting'
  | 'stale'
  | 'unavailable'
  | 'unreachable'
  | 'incompatible'
  | 'refused';

export const HOST_STATUS_KINDS: readonly HostStatusKind[] = [
  'live',
  'catching-up',
  'waiting',
  'connecting',
  'stale',
  'unavailable',
  'unreachable',
  'incompatible',
  'refused',
];

export interface HostStatus {
  readonly kind: HostStatusKind;
  /** Why the host refuses, when `kind` is `refused` and the host named it. */
  readonly cause: RefusalCause | null;
  /** How long what is shown has been out of date, when `kind` is `stale` and the age is known. */
  readonly staleForMs: number | null;
}

/** What a host's status is worked out from. */
export interface HostStatusInput {
  readonly connection: HostConnectionState;
  /** The feed the screen reads (the runs, or a cost stream). */
  readonly feed: FeedStatus;
  /** Something read earlier from that feed is on screen. */
  readonly hasData: boolean;
  /** How long the link has been down, when the service layer knows. */
  readonly staleForMs: number | null;
}

/** How bad a status is: a higher rank never reads better than a lower one. */
export const hostStatusRank = (kind: HostStatusKind): number => HOST_STATUS_KINDS.indexOf(kind);

/** The kind of a link that is up and nothing blocks: what the feed says. */
function readyKind(feed: FeedStatus, hasData: boolean): HostStatusKind {
  switch (feed._tag) {
    case 'Live':
      return 'live';
    case 'Opening':
    case 'Restarting':
      return hasData ? 'catching-up' : 'waiting';
    case 'Failed':
      return hasData ? 'stale' : 'unavailable';
  }
}

function kindOf(input: HostStatusInput): HostStatusKind {
  const {connection, feed, hasData} = input;
  switch (connection._tag) {
    case 'Unusable':
      return connection.fault.kind === 'Refused' || connection.fault.cause !== undefined
        ? 'refused'
        : 'unreachable';
    case 'Incompatible':
      return 'incompatible';
    case 'Idle':
    case 'Waiting':
      return hasData ? 'stale' : 'waiting';
    case 'Connecting':
      return hasData ? 'stale' : 'connecting';
    case 'Ready':
      return connection.blocked === undefined ? readyKind(feed, hasData) : 'refused';
  }
}

/** The status of a host, from its link, the feed read over it and what is already read. */
export function hostStatusOf(input: HostStatusInput): HostStatus {
  const kind = kindOf(input);
  return {
    kind,
    cause: kind === 'refused' ? refusalCauseOf(input.connection) : null,
    staleForMs: kind === 'stale' ? input.staleForMs : null,
  };
}
