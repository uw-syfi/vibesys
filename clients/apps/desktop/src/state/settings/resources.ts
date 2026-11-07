/** What the Resources tab shows of each host the app is connected to, as plain facts. */
import type {HostConnectionState} from '../connection.js';
import type {RefusalCause} from '../fault.js';
import type {FeedStatus} from '../feed.js';
import {type Freshness, freshnessOf} from '../freshness.js';

/** How the link to a host stands, in the words a person can act on. */
export type ResourceConnection =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'incompatible'
  | 'refused'
  | 'unreachable';

export interface ResourceRow {
  /** The `HostKey`: stable, and what a route names. */
  readonly key: string;
  /** What the host is called on screen. */
  readonly label: string;
  readonly connection: ResourceConnection;
  /** Why the host refuses for good, when `connection` is `refused`: what to change, not a retry. */
  readonly cause: RefusalCause | null;
  readonly freshness: Freshness;
}

/** What a host's row is built from. */
export interface ResourceHost {
  readonly key: string;
  readonly label: string;
  readonly connection: HostConnectionState;
  /** The run stream's status. */
  readonly runsFeed: FeedStatus;
  /** Something read earlier from the host is on screen. */
  readonly hasData: boolean;
}

function connectionOf(state: HostConnectionState): ResourceConnection {
  switch (state._tag) {
    case 'Idle':
      return 'idle';
    case 'Connecting':
      return 'connecting';
    case 'Ready':
      return 'connected';
    case 'Waiting':
      return 'reconnecting';
    case 'Incompatible':
      return 'incompatible';
    case 'Unusable':
      return state.fault.kind === 'Refused' ? 'refused' : 'unreachable';
  }
}

export const resourceRow = (host: ResourceHost): ResourceRow => ({
  key: host.key,
  label: host.label,
  connection: connectionOf(host.connection),
  cause:
    host.connection._tag === 'Unusable' && host.connection.fault.cause !== undefined
      ? host.connection.fault.cause
      : null,
  freshness: freshnessOf(host.connection, host.runsFeed, host.hasData),
});
