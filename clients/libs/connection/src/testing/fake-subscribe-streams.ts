import {create, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import type {HostKey, ListingStep} from '@vibesys/hosts';
import {
  type SubscribeRequest,
  SubscribeRequestSchema,
  SubscribeResponseSchema,
  validatedFromBinary,
  validateHostPayload,
} from '@vibesys/proto';
import {SUBSCRIBE_METHOD} from '@vibesys/subscription';
import {GrpcError, registerAbort} from '@vibesys/transport';
import type {SubscriptionStreams} from '../shell/subscriptions.js';

/** A frame as a test writes it: the case and the value's fields. */
export type Frame = NonNullable<MessageInitShape<typeof SubscribeResponseSchema>['frame']>;

/** One Subscribe stream a host's gateway holds open, with the means to speak and end it. */
export interface FakeSubscribeStream {
  readonly host: HostKey;
  readonly request: SubscribeRequest;
  readonly signal: AbortSignal;
  /** Whether the connection still holds this stream: not ended, not left. */
  readonly open: boolean;
  /** The connection's report that the stream reached `step`. */
  reached(step: ListingStep): void;
  /**
   * The gateway sends a frame. As on the real connection, a frame the schema refuses fails the
   * stream, and a stream that was left or ended hears nothing.
   */
  send(frame: Frame): void;
  end(): void;
  fail(error?: Error): void;
}

/**
 * A host's gateway as the subscription hub sees it. It holds the real connection's rules: the
 * request is the generated one, every frame is validated against the generated schema, a left
 * stream goes quiet, and a stream that is left rejects with its abort reason.
 */
export class FakeSubscribeStreams implements SubscriptionStreams {
  readonly streams: FakeSubscribeStream[] = [];

  /** The streams the hub holds open now. */
  get open(): readonly FakeSubscribeStream[] {
    return this.streams.filter(stream => stream.open);
  }

  stream(
    host: HostKey,
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
    reached: (step: ListingStep) => void,
  ): Promise<void> {
    if (method !== SUBSCRIBE_METHOD) throw new Error(`not a subscription: ${method}`);
    validateHostPayload(method, 'input', bytes);
    const request = validatedFromBinary(SubscribeRequestSchema, bytes);
    return new Promise<void>((resolve, reject) => {
      let open = true;
      const finish = (settle: () => void): void => {
        if (!open) return;
        open = false;
        settle();
      };
      const registration = registerAbort(signal, () => finish(() => reject(signal.reason)));
      if (registration.aborted) finish(() => reject(signal.reason));
      this.streams.push({
        host,
        request,
        signal,
        get open() {
          return open;
        },
        reached: step => {
          if (open) reached(step);
        },
        send: frame => {
          if (!open) return;
          const encoded = toBinary(
            SubscribeResponseSchema,
            create(SubscribeResponseSchema, {frame}),
          );
          try {
            validateHostPayload(method, 'output', encoded);
            receive(encoded);
          } catch (cause) {
            finish(() => reject(new GrpcError(15, `invalid Subscribe response: ${String(cause)}`)));
          }
        },
        end: () => finish(resolve),
        fail: error => finish(() => reject(error ?? new GrpcError(14, 'connection lost'))),
      });
    });
  }
}
