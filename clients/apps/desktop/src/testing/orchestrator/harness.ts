/**
 * A whole host session over a fake daemon, read the way a screen reads it: through a registry,
 * with atoms mounted and unmounted. Time is the ambient clock, so a test under `TestClock` moves
 * it with `elapse`.
 */
import * as Effect from 'effect/Effect';
import type * as Atom from 'effect/reactivity/Atom';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import * as TestClock from 'effect/testing/TestClock';
import {type HostSession, makeHostSession} from '../../core/host-session.js';
import {Ids} from '../../core/ids.js';
import type {HostModel} from '../../state/index.js';
import {settle} from '../settle.js';
import {
  type FaultPlan,
  faultySteps,
  makeFakeTransport,
  type Recording,
} from '../transport/index.js';
import type {FakeDaemon} from './fake-daemon.js';
import {type DaemonOptions, daemonRecording, methodPathOf} from './recordings.js';

export interface HarnessOptions extends DaemonOptions {
  /** Link faults on the run stream (Hello and the other calls are never cut). */
  readonly runStreamFaults?: FaultPlan;
}

const onlyRunStream = (base: Recording, plan: FaultPlan): Recording => ({
  respond: (target, nth) => {
    const steps = base.respond(target, nth);
    return target.method === methodPathOf('WatchRuns') ? faultySteps(steps, plan, nth) : steps;
  },
});

export const hostHarness = (daemon: FakeDaemon, options: HarnessOptions = {}) =>
  Effect.gen(function* () {
    const base = daemonRecording(daemon, options);
    const fake = makeFakeTransport(
      options.runStreamFaults === undefined ? base : onlyRunStream(base, options.runStreamFaults),
    );
    const session: HostSession = yield* makeHostSession('local').pipe(
      Effect.provide(fake.layer),
      Effect.provide(Ids.layer),
    );
    const registry = AtomRegistry.make();
    return {
      session,
      fake,
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
      /** Let `seconds` of virtual time pass, one second at a time, giving fibers their turns. */
      elapse: (seconds: number) =>
        Effect.gen(function* () {
          yield* settle;
          for (let second = 0; second < seconds; second += 1) {
            yield* TestClock.adjust('1 second');
            yield* settle;
          }
        }),
    };
  });

export type HostHarness = Effect.Success<ReturnType<typeof hostHarness>>;
