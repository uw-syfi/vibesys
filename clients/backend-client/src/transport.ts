import type {ProtocolResponse, RequestInput, ServerMessage} from './protocol.js';

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
  request(input: RequestInput): Promise<ProtocolResponse>;
  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<EventSubscription>;
  close(): Promise<void>;
}
