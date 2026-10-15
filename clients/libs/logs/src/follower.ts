/**
 * The shell around `logFollowMachine`: it provides the machine's one operation, a `logs.read` over
 * a `LogTransport`, and runs the actor on an injected clock. A view starts one follower when a
 * stream comes on screen and stops it when it leaves; stopping aborts the read in flight.
 */
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import type {AbortSignalLike} from '@vibesys/transport';
import {type Actor, createActor, fromPromise} from 'xstate';
import {
  type LogFollowInput,
  type LogReadInput,
  type LogReadOutcome,
  logFollowMachine,
} from './follow.js';

/**
 * How a follower reaches the helper's `logs.read`: on the channel the connection manager opens for
 * logs (`docs/contributing/desktop-connection.md`, "Logs"), never the control channel. It resolves
 * a helper error as `{ok: false}`; it rejects, or resolves `{ok: false, failure: {code:
 * 'transport'}}`, when the channel failed. It honors `signal`: an aborted read stops waiting and
 * its reply is dropped.
 */
export interface LogTransport {
  read(input: LogReadInput, signal: AbortSignalLike): Promise<LogReadOutcome>;
}

export type LogFollower = Actor<typeof logFollowMachine>;

/** `logFollowMachine` with its read provided by `transport`. */
export function logFollowMachineFor(transport: LogTransport): typeof logFollowMachine {
  return logFollowMachine.provide({
    actors: {
      read: fromPromise<LogReadOutcome, LogReadInput>(({input, signal}) =>
        transport.read(input, signal),
      ),
    },
  });
}

/** Start following one stream: it reads the tail at once, then follows until stopped. */
export function startLogFollower(
  transport: LogTransport,
  schedule: ScheduleTimeout,
  input: LogFollowInput,
): LogFollower {
  const actor = createActor(logFollowMachineFor(transport), {input, clock: clockFrom(schedule)});
  actor.start();
  actor.send({type: 'open'});
  return actor;
}
