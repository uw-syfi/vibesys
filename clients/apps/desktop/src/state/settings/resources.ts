/** What the Resources tab shows of each host the app is connected to, as plain facts. */
import type {HostConnectionState} from '../connection.js';
import type {FeedStatus} from '../feed.js';
import {type Freshness, freshnessOf} from '../freshness.js';

/** How the link to a host stands, in the words a person can act on. */
export type ResourceConnection =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'incompatible'
  | 'unreachable';

export interface ResourceRow {
  /** The `HostKey`: stable, and what a route names. */
  readonly key: string;
  /** What the host is called on screen. */
  readonly label: string;
  readonly connection: ResourceConnection;
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
      return 'unreachable';
  }
}

export const resourceRow = (host: ResourceHost): ResourceRow => ({
  key: host.key,
  label: host.label,
  connection: connectionOf(host.connection),
  freshness: freshnessOf(host.connection, host.runsFeed, host.hasData),
});
