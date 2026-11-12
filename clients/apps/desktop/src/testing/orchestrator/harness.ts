/**
 * A whole host session over a fake daemon, read the way a screen reads it: through a registry,
 * with atoms mounted and unmounted. Time is the ambient clock, so a test under `TestClock` moves
 * it with `elapse`.
 */
import * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import type * as Atom from 'effect/reactivity/Atom';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import * as Scheduler from 'effect/Scheduler';
import * as TestClock from 'effect/testing/TestClock';
import {type HostSession, makeHostSession} from '../../core/host-session.js';
import {Ids} from '../../core/ids.js';
import type {HostModel} from '../../state/index.js';
import {invokeCallback} from '../callback-errors.js';
import {makeQuiescence} from '../settle.js';
import {
  type FaultPlan,
  faultySteps,
  makeFakeTransport,
  type Recording,
} from '../transport/index.js';
import type {FakeDaemon} from './fake-daemon.js';
import {type DaemonOptions, daemonRecording, methodPathOf} from './recordings.js';

export interface HarnessOptions extends DaemonOptions {
  /** Changes what the daemon's recording does, for faults the daemon options cannot express. */
  readonly wrap?: (base: Recording) => Recording;
}

/** `plan` laid over the calls to one method only (Hello and the rest are never cut). */
export const faultsOn =
  (method: string, plan: FaultPlan) =>
  (base: Recording): Recording => ({
    respond: (target, nth) => {
      const steps = base.respond(target, nth);
      return target.method === methodPathOf(method) ? faultySteps(steps, plan, nth) : steps;
    },
  });

export const hostHarness = (daemon: FakeDaemon, options: HarnessOptions = {}) =>
  Effect.gen(function* () {
    const {scheduler, settle} = yield* makeQuiescence;
    const base = daemonRecording(daemon, options);
    const wrap = options.wrap;
    const fake = makeFakeTransport(wrap === undefined ? base : invokeCallback(() => wrap(base)));
    const session: HostSession = yield* makeHostSession('local').pipe(
      Effect.provide(fake.layer),
      Effect.provide(Ids.layer),
      // Every fiber of the session runs on the scheduler `settle` watches.
      Effect.provideService(Scheduler.Scheduler, scheduler),
    );
    const registry = AtomRegistry.make();
    return {
      session,
      fake,
      /** The registry a component test can provide to React to observe this same session lifetime. */
      registry,
      /** Subscribe like a mounted screen; calling the result unmounts. */
      mount: <A>(atom: Atom.Atom<A>): (() => void) => registry.mount(atom),
      /** The atom's value after the fibers that feed it have had their turn. */
      read: <A>(atom: Atom.Atom<A>) =>
        Effect.gen(function* () {
          yield* settle;
          return registry.get(atom);
        }),
      model: Effect.gen(function* () {
        yield* settle;
        return registry.get(session.model) as HostModel;
      }),
      /** Let `seconds` of virtual time pass in steps, giving fibers their turns after each. */
      elapse: (seconds: number, stepSeconds = 1) =>
        Effect.gen(function* () {
          yield* settle;
          for (let passed = 0; passed < seconds; passed += stepSeconds) {
            yield* TestClock.adjust(Duration.seconds(stepSeconds));
            yield* settle;
          }
        }),
    };
  });

export type HostHarness = Effect.Success<ReturnType<typeof hostHarness>>;
