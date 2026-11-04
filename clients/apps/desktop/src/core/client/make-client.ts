/**
 * A typed client for any protobuf service, over a `GatewayTransport`. A method is an `Effect`
 * (unary) or a `Stream` (server streaming); both fail with an `AppError`, validate the request
 * before sending and every reply frame before emitting it, and are cancelled by interruption.
 */
import type {DescMessage, DescMethod, MessageInitShape, MessageShape} from '@bufbuild/protobuf';
import type {GenService, GenServiceMethods} from '@bufbuild/protobuf/codegenv2';
import * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Stream from 'effect/Stream';
import type {HostKey} from '../../state/index.js';
import {type AppError, DeadlineExceeded, fromTransportError} from '../errors.js';
import {GatewayTransport} from '../transport/gateway-transport.js';
import {decodeResponse, encodeRequest} from './validate.js';

/** The most unread frames a stream holds before it is cut (one snapshot is one frame). */
export const STREAM_CAPACITY = 2048;
/** The longest a unary call waits: the gateway ends it at the same age. */
export const UNARY_DEADLINE: Duration.Input = '15 seconds';
/** The longest a stream waits for its first frame. */
export const FIRST_FRAME_DEADLINE: Duration.Input = '15 seconds';

export interface CallOptions {
  /** How long a unary call, or a stream's first frame, may take. */
  readonly deadline?: Duration.Input;
}

export type EffectClient<M extends GenServiceMethods> = {
  readonly [K in keyof M]: M[K]['methodKind'] extends 'unary'
    ? (
        request: MessageInitShape<M[K]['input']>,
        options?: CallOptions,
      ) => Effect.Effect<MessageShape<M[K]['output']>, AppError>
    : M[K]['methodKind'] extends 'server_streaming'
      ? (
          request: MessageInitShape<M[K]['input']>,
          options?: CallOptions,
        ) => Stream.Stream<MessageShape<M[K]['output']>, AppError>
      : never;
};

const methodPath = (service: {readonly typeName: string}, method: DescMethod): string =>
  `/${service.typeName}/${method.name}`;

/** Fail `Stream` with `onTimeout` unless its first element arrives within `duration`. */
const timeoutFirst = <A, E, R>(
  self: Stream.Stream<A, E, R>,
  duration: Duration.Input,
  onTimeout: () => E,
): Stream.Stream<A, E, R> =>
  Stream.unwrap(
    Effect.gen(function* () {
      const pull = yield* Stream.toPull(self);
      let first = true;
      return Stream.fromPull(
        Effect.succeed(
          Effect.suspend(() => {
            if (!first) return pull;
            first = false;
            return Effect.timeoutOrElse(pull, {duration, orElse: () => Effect.fail(onTimeout())});
          }),
        ),
      );
    }),
  );

/** The client of `service` on `host`. Needs the transport; owns no state. */
export const makeClient = <M extends GenServiceMethods>(
  service: GenService<M>,
  host: HostKey,
): Effect.Effect<EffectClient<M>, never, GatewayTransport> =>
  Effect.gen(function* () {
    const transport = yield* GatewayTransport;

    const unary = (method: DescMethod) => (init: unknown, options?: CallOptions) => {
      const type = method.output.typeName;
      const deadline = options?.deadline ?? UNARY_DEADLINE;
      return Effect.gen(function* () {
        const request = yield* encodeRequest(method.input, init as MessageInitShape<DescMessage>);
        const reply = yield* transport
          .unary({host, method: methodPath(service, method), request})
          .pipe(
            Effect.mapError(error => fromTransportError(error, type)),
            Effect.timeoutOrElse({
              duration: deadline,
              orElse: () =>
                Effect.fail(
                  new DeadlineExceeded({
                    origin: 'client',
                    waited: Duration.fromInputUnsafe(deadline),
                  }),
                ),
            }),
          );
        return yield* decodeResponse(method.output, reply);
      });
    };

    const serverStream = (method: DescMethod) => (init: unknown, options?: CallOptions) => {
      const type = method.output.typeName;
      const deadline = options?.deadline ?? FIRST_FRAME_DEADLINE;
      return Stream.unwrap(
        encodeRequest(method.input, init as MessageInitShape<DescMessage>).pipe(
          Effect.map(request =>
            timeoutFirst<Uint8Array, AppError, never>(
              transport
                .stream({host, method: methodPath(service, method), request}, STREAM_CAPACITY)
                .pipe(Stream.mapError(error => fromTransportError(error, type))),
              deadline,
              () =>
                new DeadlineExceeded({
                  origin: 'client',
                  waited: Duration.fromInputUnsafe(deadline),
                }),
            ).pipe(Stream.mapEffect(frame => decodeResponse(method.output, frame))),
          ),
        ),
      );
    };

    const methods: Record<string, unknown> = {};
    for (const method of service.methods) {
      if (method.methodKind === 'unary') methods[method.localName] = unary(method);
      else if (method.methodKind === 'server_streaming')
        methods[method.localName] = serverStream(method);
    }
    return methods as EffectClient<M>;
  });
