/**
 * The gateway as the window reaches it: one port per call, authorized before anything is
 * provisioned or dialed, answered by `GatewayCalls`. This is the one place a Promise-and-abort-signal
 * world (the port lifecycle) meets the Effect world (the calls): `run` is the entry point's own
 * runner, so no Effect is run from here.
 */
import * as Cause from 'effect/Cause';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import {GATEWAY_CHANNEL, gatewayReply, parseGatewayRequest} from '../core/gateway-codec.js';
import type {CallError, GatewayCalls} from './gateway-calls.js';
import {type PortEvent, type PortIpc, servePort} from './port-ipc.js';

/** Run a call's effect to its exit; interrupting `signal` interrupts the effect. */
export type RunCall = (
  effect: Effect.Effect<void, CallError>,
  signal: AbortSignal,
) => Promise<Exit.Exit<void, CallError>>;

export function installGatewayIpc(
  ipc: PortIpc,
  {calls, admits, run}: {calls: GatewayCalls; admits: (event: PortEvent) => boolean; run: RunCall},
): void {
  servePort(ipc, GATEWAY_CHANNEL, admits, async (value, signal, send) => {
    const request = parseGatewayRequest(value);
    const receive = (bytes: Uint8Array): void => send(gatewayReply(bytes));
    const call =
      request.kind === 'unary'
        ? Effect.map(calls.unary(request.host, request.method, request.bytes), receive)
        : calls.stream(request.host, request.method, request.bytes, receive);
    const exit = await run(call, signal);
    if (Exit.isSuccess(exit)) return;
    // The typed failure ends the call with its own words and status; an interruption (the window
    // went away) has no one left to read it.
    const failure = Cause.findErrorOption(exit.cause);
    throw failure._tag === 'Some' ? failure.value : new Error('gateway caller cancelled');
  });
}
