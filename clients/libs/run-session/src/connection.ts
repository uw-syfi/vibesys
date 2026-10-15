/** What a live page knows about its connection to the server: a value, no I/O. */
import {ServerError} from '@vibesys/contracts';
import type {ControlChannelState} from '@vibesys/transport';

/** Whether a fenced snapshot failed because its stream store was replaced. */
export function isSnapshotStoreReplaced(error: unknown): error is Error {
  return error instanceof ServerError && error.diagnostic?.code === 'snapshot_store_replaced';
}

/** Whether a fenced iteration page belongs to a stream store the server has replaced. */
export function isIterationStoreReplaced(error: unknown): error is Error {
  return error instanceof ServerError && error.diagnostic?.code === 'iteration_store_replaced';
}

/** Whether an old server rejected a request it could not decode before typed diagnostics existed. */
export function isLegacyUnsupportedRequest(error: unknown): error is ServerError {
  return error instanceof ServerError && error.diagnostic === null;
}

export type WebSessionStatus = 'connecting' | 'connected' | 'stale';

/** What the page renders about its connection. */
export interface WebSessionState {
  /**
   * Whether a valid run snapshot has been applied to the run fold. Monotone for the session's
   * lifetime: a later refresh failure does not make the run state already on screen unknown again.
   *
   * The render uses this boundary to distinguish its own connection state from the run's
   * lifecycle. Before the first snapshot, run-state's `connecting` is only its initial value and
   * must not be presented as a run status.
   */
  readonly hasSnapshot: boolean;
  /** The event stream: whether the transcript on screen is still growing. */
  readonly status: WebSessionStatus;
  readonly error: Error | null;
  /** The optional policy-navigation query failed; independent of event-stream health. */
  readonly iterationError: Error | null;
  /**
   * Whether a control request issued now can be delivered, as the transport's control channel
   * reports it. A different fact from a stale transcript: the stream and the command path fail
   * independently, and only this one says the request/response half is inert. Today that costs
   * the page its `query.snapshot`, so a reattach cannot reload the run's state.
   *
   * `connected` until the channel reports otherwise, because there is nothing to report before the
   * first dial settles. That is a starting value, not a claim: start-up issues `query.snapshot`, so
   * a cold-start failure arrives as a `disconnected` report like any other.
   *
   * An outage is reported whenever the channel reports one, including on a run that has already
   * ended. Whether it is worth showing then is the frontend's decision, and `connectionBanners`
   * makes it: judging it here would latch, because the control drop and the run's terminal event
   * arrive over two independent sockets in either order.
   */
  readonly controls: ControlChannelState;
}
