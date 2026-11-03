/** Bytes in, bytes out: how a call reaches a host. It knows no service, message or domain. */

import type {HostKey} from '@vibesys/hosts';
import * as Context from 'effect/Context';
import type * as Effect from 'effect/Effect';
import type * as Stream from 'effect/Stream';
import type {CallEnded, NoBridge, StreamOverflow} from '../errors.js';

/** One call: which host, which method (`/<service full name>/<method>`), and the request bytes. */
export interface CallTarget {
  readonly host: HostKey;
  readonly method: string;
  readonly request: Uint8Array;
}

/**
 * Calls over the gateway. Interrupting a call or a stream's consumer cancels it at the gateway,
 * at most once, and nothing is delivered afterwards.
 */
export class GatewayTransport extends Context.Service<
  GatewayTransport,
  {
    /** The reply bytes, or how the call ended. */
    readonly unary: (target: CallTarget) => Effect.Effect<Uint8Array, CallEnded | NoBridge>;
    /**
     * The frames in order. A clean close ends the stream; a failed end fails it with `CallEnded`.
     * Holds at most `capacity` unread frames; one more fails the stream with `StreamOverflow` and
     * cancels the call.
     */
    readonly stream: (
      target: CallTarget,
      capacity: number,
    ) => Stream.Stream<Uint8Array, CallEnded | NoBridge | StreamOverflow>;
  }
>()('app/GatewayTransport') {}
