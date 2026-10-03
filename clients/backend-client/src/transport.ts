import type {ProtocolResponse, RequestInput, ServerMessage} from './protocol.js';
import type {RequestOptions} from './request-policy.js';

export interface EventSubscription {
  close(): Promise<void>;
}

export interface SubscribeOptions {
  /**
   * Replay at most this many of the newest events instead of the whole history.
   * A server that predates the field forbids it and rejects the subscription,
   * which is exactly how a caller probes for the capability.
   */
  tail?: number;
  /**
   * The store the caller's `afterSequence` numbers, carried by a resume so the
   * server can tell whether that cursor still belongs to the live store. Sent
   * only when non-empty; a server that predates the field forbids it and
   * rejects the subscription, so the caller falls back to a plain resume.
   */
  storeId?: string;
}

/**
 * The subscribe request the options above encode, as the generated union's own
 * member rather than an object literal each transport asserts its way into.
 *
 * One place owns the encoding rules the two option docs state, so the
 * transports cannot disagree about them, which they did: one omitted an empty
 * `storeId` while the other sent `store_id: ""`.
 *
 * - An option the caller did not set is omitted, never sent as `null`. A server
 *   that predates a field forbids unknown keys, so a default subscribe has to
 *   stay byte for byte what it has always been, and `store_id` in particular is
 *   rejected as an explicit `null` while being accepted as omitted: the server
 *   model is `store_id: str = ""` and the generated type is `store_id?: string`,
 *   optional and not nullable. Returning the union member makes `null`
 *   unrepresentable here instead of guarded against at each call site.
 * - An empty `storeId` says the caller has not seen a store yet, which is
 *   absence, so it is omitted as well.
 */
export function subscribeRequest(
  afterSequence: number,
  options: SubscribeOptions,
): Extract<RequestInput, {type: 'subscribe'}> {
  return {
    type: 'subscribe',
    after_sequence: afterSequence,
    ...(options.tail === undefined ? {} : {tail: options.tail}),
    ...(options.storeId === undefined || options.storeId === '' ? {} : {store_id: options.storeId}),
  };
}

/**
 * The transport surface a session consumes: one control request/response verb,
 * a live event subscription, and a bounded close. `ServerClient` (the Node
 * socket transport) satisfies it structurally, and a browser WebSocket
 * transport will too, so nothing above this line depends on how the bytes
 * travel. Neutral by construction: it names only protocol types, no runtime.
 */
export interface ServerTransport {
  /**
   * Send one control request. `options` are the per-call half of the request
   * policy (`request-policy.ts`): the deadline, whether it takes a connection
   * of its own, and the signal that abandons it. Every implementation honors
   * all three, so a caller can be written once and stay correct; nothing above
   * this line branches on a request type to get the same effect.
   *
   * Two things `timeoutMs` does not mean. It is ignored when the call runs on
   * its own connection, because such a call is bounded by the work it drives
   * rather than by the control RPC deadline (`resolveRequestPolicy`). And it
   * bounds only the on-the-wire phase: a request that arrives while the channel
   * is down is held for the recovery with no deadline armed, so total call
   * latency is bounded by the redial schedule, not by this.
   */
  request(input: RequestInput, options?: RequestOptions): Promise<ProtocolResponse>;
  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<EventSubscription>;
  close(): Promise<void>;
}

/**
 * A transport whose control channel can be redialed on demand. Separate from
 * `ServerTransport` rather than folded into it: a frontend that offers a
 * reconnect affordance needs this, and one that does not (the TUI today) should
 * not have to implement a verb it never calls. Both shipped transports satisfy
 * it, so the affordance is reachable without an optional method, which would
 * make "does my transport actually redial" a per-implementation question.
 */
export interface ControlTransport extends ServerTransport {
  /**
   * Dial the control channel now, whatever its backoff schedule was going to
   * do. See `ControlChannel.reconnect`: it cancels an armed redial rather than
   * waiting it out, so a user-visible reconnect control always shortens the
   * outage, and it no-ops on a healthy channel.
   */
  reconnect(): void;
}
