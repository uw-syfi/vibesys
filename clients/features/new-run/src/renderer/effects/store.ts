/**
 * The New run screen's shell: its machine, with each operation provided by a welcome bridge call,
 * seen as a store the screen renders. A started run ends the machine; the store then hands the run
 * on and starts a fresh form, prefilled from the runs this user opened.
 */
import type {HostKey} from '@vibesys/hosts';
import {settled, type WelcomeBridge, type WelcomeResult} from '@vibesys/ipc';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import type {RecentRun, RunRef} from '@vibesys/runs';
import {createListenerDelivery, type Store} from '@vibesys/store';
import {type Actor, createActor, fromPromise} from 'xstate';
import {type NewRunEvent, type NewRunSnapshot, newRunMachine} from '../../state/index.js';

/** The bridge calls the New run screen makes. */
export type NewRunBridge = Pick<
  WelcomeBridge,
  | 'describeMachine'
  | 'harnesses'
  | 'examples'
  | 'tasks'
  | 'preflight'
  | 'createFromExample'
  | 'copyOut'
  | 'startRun'
  | 'helperFeatures'
>;

/** What `call` answered, or a rejection with its error (a call that throws rejects too). */
async function value<T>(call: () => Promise<WelcomeResult<T>>): Promise<T> {
  const result = await settled(call());
  if (!result.ok) throw new Error(result.error);
  return result.value;
}

/** The machine with every operation provided by `bridge`. */
export function newRunLogic(bridge: NewRunBridge) {
  return newRunMachine.provide({
    actors: {
      describe: fromPromise(({input}) => value(() => bridge.describeMachine(input.host))),
      features: fromPromise(({input}) => value(() => bridge.helperFeatures(input.host))),
      harnesses: fromPromise(async ({input}) => {
        const listing = await value(() => bridge.harnesses(input.host));
        return listing.kind === 'listed' ? listing.harnesses : [];
      }),
      examples: fromPromise(({input}) => value(() => bridge.examples(input.host))),
      tasks: fromPromise(({input}) => value(() => bridge.tasks(input.host, input.project))),
      preflight: fromPromise(({input}) => value(() => bridge.preflight(input.host, input.inputs))),
      fix: fromPromise(({input}) =>
        value(() =>
          input.fix.kind === 'copy_out'
            ? bridge.copyOut(input.host, input.fix.source, input.fix.destination)
            : bridge.createFromExample(input.host, input.fix.example, input.fix.destination),
        ),
      ),
      start: fromPromise(({input}) => value(() => bridge.startRun(input.host, input.inputs))),
    },
  });
}

/** The machine the form starts on: the one of the run opened last, else this one. */
export function initialMachine(recent: readonly RecentRun[]): HostKey {
  const last = recent.reduce<RecentRun | null>(
    (best, run) => (best === null || run.attachedAt > best.attachedAt ? run : best),
    null,
  );
  return last?.host ?? 'local';
}

export interface NewRunStoreOptions {
  readonly recent: readonly RecentRun[];
  /** Where the machine's delays (the checks' debounce) are scheduled. */
  readonly schedule: ScheduleTimeout;
  /** Hears each run the screen started. */
  readonly started: (run: RunRef) => void;
}

/**
 * The New run screen's store over `bridge`: a running machine, replaced by a fresh one (on the same
 * machine, with the runs opened since) after each start; `stop` ends its work.
 */
export function createNewRunStore(
  bridge: NewRunBridge,
  options: NewRunStoreOptions,
): {readonly store: Store<NewRunSnapshot, NewRunEvent>; readonly stop: () => void} {
  const logic = newRunLogic(bridge);
  const clock = clockFrom(options.schedule);
  const delivery = createListenerDelivery();
  let recent = options.recent;
  // Until the user changes an input, the form follows the runs opened (the first of which may
  // arrive after the store was made), so it is prefilled from the last run, on its machine.
  let edited = false;
  const spawn = (machine: HostKey): Actor<typeof logic> => {
    const actor = createActor(logic, {input: {machine, recent}, clock});
    actor.subscribe(snapshot => {
      const run = snapshot.context.started;
      if (snapshot.status === 'done' && run !== null) {
        edited = false;
        current = spawn(snapshot.context.form.machine);
        options.started({host: snapshot.context.form.machine, runId: run.runId});
      }
      delivery.notify();
    });
    actor.start();
    return actor;
  };
  let current = spawn(initialMachine(recent));
  const store: Store<NewRunSnapshot, NewRunEvent> = {
    getState: () => current.getSnapshot(),
    subscribe: delivery.subscribe,
    dispatch: event => {
      if (event.type !== 'recent') {
        edited = true;
        current.send(event);
        return;
      }
      recent = event.recent;
      if (edited) {
        current.send(event);
        return;
      }
      current.stop();
      current = spawn(initialMachine(recent));
      delivery.notify();
    },
  };
  return {store, stop: () => current.stop()};
}
