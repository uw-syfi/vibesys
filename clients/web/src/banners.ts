import type {ControlChannelState} from '@vibesys/backend-client';
import {type CoreState, hasRunEnded} from '@vibesys/core-state';
import type {WebSessionState} from './session.js';

/** The control-channel outage a banner describes, once there is one to show. */
type ControlOutage = Extract<ControlChannelState, {status: 'disconnected'}>;

/** What the controls banner says and what its button can do. */
interface ControlsBanner {
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
   * The controls banner to render, or `null` for no banner. Carries its copy
   * rather than a flag, so the text is decided somewhere a unit test can read
   * it instead of only inside JSX.
   *
   * Null once the run has ended: the gateway going away is how a finished run
   * ends rather than a fault, and an affordance shown then asks the user to fix
   * a problem they do not have.
   */
  readonly controls: ControlsBanner | null;
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
 * The consequence named is the true one. `query.snapshot` is the only request
 * `clients/web` issues, so a dead channel means the run's state cannot be
 * reloaded; pause, resume, steer, and chat are not affordances this client has,
 * and they arrive with #815, which owns the web run controls.
 *
 * Exported because two other places have to agree with it, and retyping the
 * string is how they stop agreeing: `banners.test.ts` asserts which of the two
 * a given state selects, and `e2e/controls-banner.spec.ts` asserts that the
 * rendered page says it. Both import from here. An e2e spec that kept its own
 * copy asserted text this module had already replaced, and the mismatch read as
 * a behavior failure rather than as drift.
 */
export const CONTROLS_BANNER_COPY = {
  lost: 'Controls lost their connection to the run. Its state cannot be refreshed until they reconnect.',
  cold: 'Controls have not reached the run. Its state cannot be refreshed until they connect.',
} as const;

function describeOutage(outage: ControlOutage): string {
  return CONTROLS_BANNER_COPY[outage.everConnected ? 'lost' : 'cold'];
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
    controls:
      ended || outage === null
        ? null
        : {message: describeOutage(outage), retrying: outage.retrying},
  };
}
