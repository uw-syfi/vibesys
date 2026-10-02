import type {ControlChannelState} from '@vibesys/backend-client';
import {hasRunEnded} from '@vibesys/core-state';
import type {WorkspaceState} from './session.js';

/** The control-channel outage a banner describes, once there is one to show. */
type ControlOutage = Extract<ControlChannelState, {status: 'disconnected'}>;

/** What the controls banner says and what its button can do. */
export interface ControlsBanner {
  /**
   * What is wrong, in the page's own words. Composed here rather than by
   * interpolating `error.message`, which is a transport string ("WebSocket
   * transport error") that names neither what the user lost nor what to do.
   */
  readonly message: string;
  /**
   * Whether a dial is already in flight, so `reconnect()` would no-op. The
   * button stays rendered and goes disabled, because a click that silently does
   * nothing is worse than a button that says it is already working.
   */
  readonly retrying: boolean;
}

/**
 * What the controls banner says, by whether the channel ever had a connection.
 *
 * Two cases, because they are different failures and read differently: a channel
 * that never reached the gateway at all, and one that lost a connection it had.
 * `error.kind` could refine this further (a `parse` fault will not survive a
 * redial, unlike a `disconnected` one), but every kind costs the user the same
 * thing today, so the copy does not branch on it yet.
 *
 * Exported because two other places have to agree with it, and retyping the
 * string is how they stop agreeing: `banners.test.ts` asserts which of the two
 * a given state selects, and `e2e/controls-banner.spec.ts` asserts that the
 * rendered page says it. Both import from here. An e2e spec that kept its own
 * copy asserted text this module had already replaced, and the mismatch read as
 * a behavior failure rather than as drift.
 */
export const CONTROLS_BANNER_COPY = {
  lost: 'Controls lost their connection to the run. Its commands and queries cannot be delivered until they reconnect.',
  cold: 'Controls have not reached the run. Its commands and queries cannot be delivered until they connect.',
} as const;

function describeOutage(outage: ControlOutage): string {
  return CONTROLS_BANNER_COPY[outage.everConnected ? 'lost' : 'cold'];
}

/**
 * The controls banner to render, or `null` for no banner. Carries its copy
 * rather than a flag, so the text is decided somewhere a unit test can read it
 * instead of only inside JSX.
 *
 * This is a presentation judgment, not a fact about the transport, so it lives
 * here and not in `WorkspaceSession`: the session reports that the control
 * channel is undeliverable, which stays true after the run ends, and this
 * decides whether that is worth showing. Deciding it at report time instead
 * latches, because a control drop and the run's terminal event arrive over two
 * independent sockets in either order, and only a render re-runs when the later
 * of the two lands.
 *
 * Null once the run has ended: the gateway going away is how a finished run
 * ends rather than a fault, and an affordance shown then asks the user to fix a
 * problem they do not have.
 *
 * The event stream's own staleness is not decided here. `WorkspaceState` already
 * carries `connection`, `connectionError` and `canRetry`, and `ui/Banner.tsx`
 * renders them; a second decision over the same fields would be a second source
 * of truth for one fact.
 */
export function controlsBanner(
  state: Pick<WorkspaceState, 'core' | 'controls'>,
): ControlsBanner | null {
  if (state.controls.status !== 'disconnected' || hasRunEnded(state.core)) return null;
  return {message: describeOutage(state.controls), retrying: state.controls.retrying};
}
