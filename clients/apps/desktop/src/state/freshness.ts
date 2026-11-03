/** How much a screen should trust what it shows, from the connection and the feed it reads. */
import type {HostConnectionState} from './connection.js';
import type {FeedStatus} from './feed.js';

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
