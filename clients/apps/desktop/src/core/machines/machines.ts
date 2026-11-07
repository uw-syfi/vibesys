/**
 * The machines the window can add and remove, as it sees them: a role with a real implementation
 * over the preload's bridge to the main process, which reads the ssh config and keeps the saved
 * hosts, and a Fake in `testing`. A request the main process refuses fails with `MachinesError`
 * carrying the reason the person reads.
 */
import * as Context from 'effect/Context';
import * as Data from 'effect/Data';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import {
  type MachineReply,
  type MachineRequest,
  type MachinesBridge,
  type MachineUndo,
  machineReplyFromBytes,
  machineRequestToBytes,
} from './machines-codec.js';

/** A request about machines failed; `detail` is the reason, in words the person can act on. */
export class MachinesError extends Data.TaggedError('MachinesError')<{
  readonly detail: string;
}> {}

/** The host a save made or found. */
export interface MachineAdded {
  readonly host: string;
  /** False when the host was already saved. */
  readonly created: boolean;
}

export interface MachinesApi {
  /** The ssh config aliases that match what was typed, best first. */
  readonly match: (query: string) => Effect.Effect<readonly string[], MachinesError>;
  /** Saves the alias or pasted ssh command; the command is parsed and never run. */
  readonly add: (input: string) => Effect.Effect<MachineAdded, MachinesError>;
  /** Forgets a saved host, and says how to put it back. */
  readonly remove: (host: string) => Effect.Effect<MachineUndo, MachinesError>;
  /** Puts back what a removal took. */
  readonly restore: (undo: MachineUndo) => Effect.Effect<MachineAdded, MachinesError>;
}

export class Machines extends Context.Service<Machines, MachinesApi>()('app/Machines') {}

const describe = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

const NO_BRIDGE = 'the window has no machines bridge';

/** One request to the main process and its reply; a refusal is the failure. */
export type AskMachines = (request: MachineRequest) => Effect.Effect<MachineReply, MachinesError>;

/** The calls of `Machines` over `ask`, each checking that the reply answers its request. */
export function machinesApiOver(ask: AskMachines): MachinesApi {
  const unexpected = new MachinesError({detail: 'the reply does not answer the request'});
  const added = (request: MachineRequest): Effect.Effect<MachineAdded, MachinesError> =>
    Effect.flatMap(ask(request), reply =>
      reply.kind === 'added'
        ? Effect.succeed({host: reply.host, created: reply.created})
        : Effect.fail(unexpected),
    );
  return {
    match: query =>
      Effect.flatMap(ask({op: 'match', query}), reply =>
        reply.kind === 'matches' ? Effect.succeed(reply.aliases) : Effect.fail(unexpected),
      ),
    add: input => added({op: 'add', input}),
    remove: host =>
      Effect.flatMap(ask({op: 'remove', host}), reply =>
        reply.kind === 'removed' ? Effect.succeed(reply.undo) : Effect.fail(unexpected),
      ),
    restore: undo => added({op: 'restore', undo}),
  };
}

/** `Machines` over the bridge; without one every call fails with `MachinesError`. */
export const machinesOver = (bridge: MachinesBridge | undefined): Layer.Layer<Machines> => {
  const ask: AskMachines = request =>
    Effect.callback<MachineReply, MachinesError>(resume => {
      const failed = (detail: string) => resume(Effect.fail(new MachinesError({detail})));
      if (bridge === undefined) return failed(NO_BRIDGE);
      let bytes: Uint8Array;
      try {
        bytes = machineRequestToBytes(request);
      } catch (cause) {
        return failed(describe(cause));
      }
      bridge.call(
        bytes,
        reply => {
          try {
            const decoded = machineReplyFromBytes(reply);
            if (decoded.kind === 'refused') failed(decoded.reason);
            else resume(Effect.succeed(decoded));
          } catch (cause) {
            failed(describe(cause));
          }
        },
        detail => failed(detail),
      );
    });
  return Layer.succeed(Machines, Machines.of(machinesApiOver(ask)));
};

/** The handle the preload exposes on the window, if it has one, read once when the layer is built. */
export const MachinesFromWindow: Layer.Layer<Machines> = Layer.unwrap(
  Effect.sync(() => machinesOver(globalThis.vibesysMachines)),
);
