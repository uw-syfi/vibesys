import type {ControlChannelState} from '@vibesys/backend-client';
import {type CoreState, hasRunEnded} from '@vibesys/core-state';
import type {WebSessionState} from './session.js';

/** The control-channel outage a banner describes, once there is one to show. */
type ControlOutage = Extract<ControlChannelState, {status: 'disconnected'}>;

/**
 * Which connectivity banners a render puts on screen, and what they carry. One
 * value rather than three loose predicates, so the whole decision has one site
 * and a render reads it instead of re-deciding any part of it.
 *
 * A field is set only while the thing it describes is worth acting on, which is
 * why a finished run suppresses two of them: nothing about a run that has
 * reached a status it never leaves is recovered by reconnecting to it.
 */
export interface ConnectionBanners {
  /**
   * The event stream is stale, so the transcript on screen may be short of the
   * run's tail. Shown on an ended run too: the gap is real either way.
   */
  readonly stream: boolean;
  /**
   * Offer `Reattach` inside the stream banner. Only a run that can still
   * produce an event can be resubscribed, and `WebSession.reattach` declines an
   * ended one, so offering it there would be offering a no-op.
   */
  readonly reattach: boolean;
  /**
   * The outage to describe in the controls banner, or `null` for no banner.
   * Carries the state rather than a flag so the render reads the error off one
   * narrowed value instead of testing the status a second time to reach it.
   *
   * Null once the run has ended: the gateway going away is how a finished run
   * ends rather than a fault, and an affordance shown then asks the user to fix
   * a problem they do not have.
   */
  readonly controls: ControlOutage | null;
}

/**
 * Decide the banners from the run's state and the session's.
 *
 * This is a presentation judgment, not a fact about the transport, so it lives
 * here and not in `WebSession`: the session reports that the control channel is
 * undeliverable, which stays true after the run ends, and this decides whether
 * that is worth showing. Deciding it at report time instead latches, because a
 * control drop and the run's terminal event arrive over two independent sockets
 * in either order, and only a render re-runs when the later of the two lands.
 */
export function connectionBanners(run: CoreState, session: WebSessionState): ConnectionBanners {
  const ended = hasRunEnded(run);
  const stream = session.status === 'stale';
  const outage = session.controls.status === 'disconnected' ? session.controls : null;
  return {
    stream,
    reattach: stream && !ended,
    controls: ended ? null : outage,
  };
}
