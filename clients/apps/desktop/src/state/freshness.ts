/** How much a screen should trust what it shows, from the connection and the feed it reads. */
import type {HostConnectionState} from './connection.js';
import type {FeedStatus} from './feed.js';

/**
 * Whether the link to a host is delivering, as a screen shows it for the whole host:
 * - `live`: the connection is Ready, so what the host says arrives as it happens.
 * - `reconnecting`: the link was lost or never came up, and a dial is in flight.
 * - `stale`: the link is down and no dial is in flight (waiting to redial, or stopped until the
 *   user retries); whatever is on screen was last current at `lastUpdateMs`.
 */
export type LinkStatus = 'live' | 'reconnecting' | 'stale';

/** What the service layer knows of a host's freshness; screens show it and compute nothing. */
export interface HostFreshness {
  readonly status: LinkStatus;
  /** The Clock time (ms) the host last said anything, or null when it never has. */
  readonly lastUpdateMs: number | null;
}

/** The freshness of a host whose connection is `connection` and who was last heard at `lastUpdateMs`. */
export function hostFreshness(
  connection: HostConnectionState,
  lastUpdateMs: number | null,
): HostFreshness {
  switch (connection._tag) {
    case 'Ready':
      return {status: 'live', lastUpdateMs};
    case 'Connecting':
      return {status: 'reconnecting', lastUpdateMs};
    case 'Idle':
    case 'Waiting':
    case 'Incompatible':
    case 'Unusable':
      return {status: 'stale', lastUpdateMs};
  }
}

/**
 * How long what is on screen has been out of date at `nowMs`: null while the link is live, and
 * null when nothing was ever read (there is no age to show). Never negative.
 */
export function staleForMs(freshness: HostFreshness, nowMs: number): number | null {
  if (freshness.status === 'live' || freshness.lastUpdateMs === null) return null;
  return Math.max(0, nowMs - freshness.lastUpdateMs);
}

/**
 * - `live`: connected and the feed is delivering.
 * - `catching-up`: connected, the feed is opening or restarting, and earlier data is on screen.
 * - `stale`: data is on screen that may be out of date (the link is down, or the feed failed).
 * - `empty`: nothing has been read yet and nothing is wrong yet.
 * - `unavailable`: nothing can be read until the user acts (incompatible build, failed feed).
 */
export type Freshness = 'live' | 'catching-up' | 'stale' | 'empty' | 'unavailable';

export function freshnessOf(
  connection: HostConnectionState,
  feed: FeedStatus,
  hasData: boolean,
): Freshness {
  switch (connection._tag) {
    case 'Incompatible':
    case 'Unusable':
      return hasData ? 'stale' : 'unavailable';
    case 'Idle':
    case 'Connecting':
    case 'Waiting':
      return hasData ? 'stale' : 'empty';
    case 'Ready':
      break;
  }
  switch (feed._tag) {
    case 'Live':
      return 'live';
    case 'Opening':
    case 'Restarting':
      return hasData ? 'catching-up' : 'empty';
    case 'Failed':
      return hasData ? 'stale' : 'unavailable';
  }
}
