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
   * of its own, and the signal that abandons it. An implementation honors all
   * three, so a caller can be written once and stay correct; nothing above this
   * line branches on a request type to get the same effect.
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
