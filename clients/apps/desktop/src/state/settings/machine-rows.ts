/** What the Machines page shows of each host the app is connected to, as plain facts. */
import type {HostConnectionState} from '../connection.js';
import type {FeedStatus} from '../feed.js';
import {type HostStatus, hostStatusOf} from '../host-status.js';

export interface MachineRow {
  /** The `HostKey`: stable, and what a route names. */
  readonly key: string;
  /** What the host is called on screen. */
  readonly label: string;
  /** The one status the host has on every screen. */
  readonly status: HostStatus;
}

/** What a host's row is built from. */
export interface MachineHost {
  readonly key: string;
  readonly label: string;
  readonly connection: HostConnectionState;
  /** The run stream's status. */
  readonly runsFeed: FeedStatus;
  /** Something read earlier from the host is on screen. */
  readonly hasData: boolean;
  /** How long the link has been down, when the clock and the connection know. */
  readonly staleForMs: number | null;
}

export const machineRow = (host: MachineHost): MachineRow => ({
  key: host.key,
  label: host.label,
  status: hostStatusOf({
    connection: host.connection,
    feed: host.runsFeed,
    hasData: host.hasData,
    staleForMs: host.staleForMs,
  }),
});
