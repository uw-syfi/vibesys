/** `GatewayTransport` over the preload's gateway bridge: one MessagePort per call. */

import type {DesktopLinkProblem, DesktopRefusal} from '@vibesys/proto';
import * as Cause from 'effect/Cause';
import * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Queue from 'effect/Queue';
import * as Stream from 'effect/Stream';
import {CallEnded, NoBridge, StreamOverflow} from '../errors.js';
import {
  decodeGatewayReply,
  encodeGatewayRequest,
  type GatewayPortBridge,
  type GatewayRequest,
} from '../gateway-codec.js';
import {type CallTarget, GatewayTransport} from './gateway-transport.js';

/** The window's gateway handle, if it has one. */
export class GatewayPorts extends Context.Service<
  GatewayPorts,
  {readonly bridge: GatewayPortBridge | undefined}
>()('app/GatewayPorts') {}

/** The handle the preload exposes on the window, read once when the layer is built. */
export const GatewayPortsFromWindow: Layer.Layer<GatewayPorts> = Layer.sync(GatewayPorts, () => ({
  bridge: globalThis.vibesysGateway,
}));

/** How a call ended, as the reply stream tells it. */
type Terminal = {readonly kind: 'closed'} | {readonly kind: 'failed'; readonly end: CallEnded};

const describe = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

/**
 * Open one call. `frame` gets each reply frame in order, `terminal` the one end, after which
 * nothing is delivered. Returns how to give up on the call: it releases the port once, whenever it
 * is called, and delivers nothing afterwards. A bridge that throws, or a reply that cannot be
 * decoded, is an end.
 */
const openCall = (
  bridge: GatewayPortBridge,
  request: GatewayRequest,
  frame: (bytes: Uint8Array) => boolean,
  terminal: (how: Terminal) => void,
): (() => void) => {
  let handle: {cancel(): void} | undefined;
  let finished = false;
  const release = (): void => {
    finished = true;
    const port = handle;
    handle = undefined;
    port?.cancel();
  };
  const end = (how: Terminal): void => {
    if (finished) return;
    release();
    terminal(how);
  };
  const failed = (
    message: string,
    status?: number,
    linkProblem?: DesktopLinkProblem,
    refusal?: DesktopRefusal,
  ): Terminal => ({
    kind: 'failed',
    end: new CallEnded({
      message,
      ...(status === undefined ? {} : {status}),
      ...(linkProblem === undefined ? {} : {linkProblem}),
      ...(refusal === undefined ? {} : {refusal}),
    }),
  });
  /** Act on one decoded reply: a frame, or the end. */
  const interpret = (body: ReturnType<typeof decodeGatewayReply>): void => {
    switch (body.case) {
      case 'bytes':
        if (!frame(body.value)) release();
        break;
      case 'end': {
        const {error, status, linkProblem, refusal} = body.value;
        const clean =
          error === undefined &&
          linkProblem === undefined &&
          refusal === undefined &&
          (status === undefined || status === 0);
        end(clean ? {kind: 'closed'} : failed(error ?? '', status, linkProblem, refusal));
        break;
      }
      case undefined:
        end(failed('missing gateway reply body'));
        break;
      default:
        body satisfies never;
    }
  };
  try {
    const opened = bridge.open(
      encodeGatewayRequest(request),
      reply => {
        if (finished) return;
        try {
          interpret(decodeGatewayReply(reply));
        } catch (cause) {
          end(failed(describe(cause)));
        }
      },
      message => end(failed(message)),
    );
    if (finished) opened.cancel();
    else handle = opened;
  } catch (cause) {
    end(failed(describe(cause)));
  }
  return release;
};

const requestOf = (kind: GatewayRequest['kind'], target: CallTarget): GatewayRequest => ({
  kind,
  host: target.host,
  method: target.method,
  bytes: target.request,
});

const makeTransport = (bridge: GatewayPortBridge | undefined) => {
  const unary: GatewayTransport['Service']['unary'] = target =>
    bridge === undefined
      ? Effect.fail(new NoBridge())
      : Effect.callback<Uint8Array, CallEnded>(resume => {
          let reply: Uint8Array | undefined;
          const give = openCall(
            bridge,
            requestOf('unary', target),
            bytes => {
              if (reply !== undefined) {
                resume(
                  Effect.fail(
                    new CallEnded({message: 'unary gateway returned multiple responses'}),
                  ),
                );
                return false;
              }
              reply = bytes;
              return true;
            },
            how => {
              if (how.kind === 'failed') resume(Effect.fail(how.end));
              else if (reply === undefined)
                resume(Effect.fail(new CallEnded({message: 'missing unary response'})));
              else resume(Effect.succeed(reply));
            },
          );
          return Effect.sync(give);
        });

  const stream: GatewayTransport['Service']['stream'] = (target, capacity) =>
    bridge === undefined
      ? Stream.fail(new NoBridge())
      : Stream.callback<Uint8Array, CallEnded | StreamOverflow>(
          queue =>
            Effect.gen(function* () {
              const give = openCall(
                bridge,
                requestOf('stream', target),
                bytes => {
                  if (Queue.offerUnsafe(queue, bytes)) return true;
                  Queue.failCauseUnsafe(queue, Cause.fail(new StreamOverflow({capacity})));
                  return false;
                },
                how =>
                  how.kind === 'failed'
                    ? Queue.failCauseUnsafe(queue, Cause.fail(how.end))
                    : Queue.endUnsafe(queue),
              );
              yield* Effect.addFinalizer(() => Effect.sync(give));
            }),
          {bufferSize: capacity},
        );

  return {unary, stream};
};

/** The transport over the window's gateway; without one every call fails with `NoBridge`. */
export const GatewayTransportLive: Layer.Layer<GatewayTransport, never, GatewayPorts> =
  Layer.effect(
    GatewayTransport,
    Effect.gen(function* () {
      const ports = yield* GatewayPorts;
      return makeTransport(ports.bridge);
    }),
  );
