/** How the Machines page reads an eval place the host reported: its reach, in one status word. */
import {type EvalPlace, OffersUnreadableReason} from '@vibesys/orchestrator-proto';

export type Reach = 'reachable' | 'unreachable' | 'not probed';

/**
 * Whether the host could reach the place: known hardware means it did, a refused or timed-out probe
 * means it could not, and a place the host cannot probe (or whose hardware it could not read) was
 * not probed.
 */
export function reachOf(place: EvalPlace): Reach {
  switch (place.state.case) {
    case 'known':
      return 'reachable';
    case 'unreadable':
      return place.state.value.reason === OffersUnreadableReason.UNREACHABLE ||
        place.state.value.reason === OffersUnreadableReason.TIMED_OUT
        ? 'unreachable'
        : 'not probed';
    case undefined:
      return 'not probed';
    default:
      place.state satisfies never;
      return 'not probed';
  }
}
