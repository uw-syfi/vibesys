/**
 * The New run screen's shell: its machine, with each operation provided by a welcome bridge call,
 * seen as a store the screen renders. A started run ends the machine; the store then hands the run
 * on and starts a fresh form, prefilled from the runs this user opened.
 */
import type {CompletionAction, CompletionState, HostKey} from '@vibesys/hosts';
import {attempt, type WelcomeBridge, type WelcomeResult} from '@vibesys/ipc';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import type {RecentRun, RunRef} from '@vibesys/runs';
import {createListenerDelivery, type Store} from '@vibesys/store';
import {type Actor, createActor, fromPromise} from 'xstate';
import {
  type Draft,
  draftOf,
  type FixDone,
  type NewRunEvent,
  type NewRunSnapshot,
  newRunMachine,
  StepFailure,
  storeDraft,
} from '../../state/index.js';
import {createFolderCompletion} from './completion.js';

/** The bridge calls the New run screen makes. */
export type NewRunBridge = Pick<
  WelcomeBridge,
  | 'describeMachine'
  | 'harnesses'
  | 'examples'
  | 'tasks'
  | 'taskGoal'
  | 'preflight'
  | 'createFromExample'
  | 'exampleDestination'
  | 'copyOut'
  | 'startRun'
  | 'helperFeatures'
  | 'complete'
>;

/**
 * What `call` answered, or a `StepFailure`: with the main process's words when it reported one, with
 * none when the call threw or rejected (a bridge method that is missing, the main process gone).
 */
async function value<T>(call: () => Promise<WelcomeResult<T>>): Promise<T> {
  const result = await attempt(call);
  if (result.ok) return result.value;
  if ('unexpected' in result) throw new StepFailure(null);
  throw new StepFailure(result.error, result.cause ?? null);
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
      goal: fromPromise(({input}) =>
        value(() => bridge.taskGoal(input.host, input.project, input.task)),
      ),
      preflight: fromPromise(({input}) => value(() => bridge.preflight(input.host, input.inputs))),
      destination: fromPromise(({input}) =>
        value(() => bridge.exampleDestination(input.host, input.example, input.destination)),
      ),
      fix: fromPromise(async ({input}): Promise<FixDone> => {
        const {fix} = input;
        if (fix.kind === 'copy_out') {
          const copy = await value(() => bridge.copyOut(input.host, fix.source, fix.destination));
          return {fix, created: copy};
        }
        const made = await value(() =>
          bridge.createFromExample(input.host, fix.example, fix.destination),
        );
        return {fix, created: made.kind === 'created' ? made.created : null};
      }),
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

/** Where what the user chose for More options on each machine is kept between runs of the app. */
export interface MoreOptionsMemory {
  /** The stored choices by machine; none when nothing is stored or it cannot be read. */
  read(): Readonly<Record<string, boolean>>;
  /** Keep `choices`; a failure to store them is not an error, they just last this run. */
  write(choices: Readonly<Record<string, boolean>>): void;
}

/** Where this window keeps an unfinished New run, so leaving and coming back finds it. */
export interface DraftMemory {
  /** The stored draft; none when nothing is stored or what is stored is not a draft. */
  read(): Draft | null;
  /** Keep `draft` (null: forget it); a failure to store it is not an error, it just lasts this run. */
  write(draft: Draft | null): void;
}

export interface NewRunStoreOptions {
  readonly recent: readonly RecentRun[];
  /** Keeps the unfinished form of this window; without one it lasts the store's life. */
  readonly draft?: DraftMemory;
  /** Remembers More options per machine; without one the choices last the store's life. */
  readonly moreOptions?: MoreOptionsMemory;
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
): {
  readonly store: Store<NewRunSnapshot, NewRunEvent>;
  /** The folder field's path completion. */
  readonly completion: Store<CompletionState, CompletionAction>;
  readonly stop: () => void;
} {
  const logic = newRunLogic(bridge);
  const clock = clockFrom(options.schedule);
  const delivery = createListenerDelivery();
  let recent = options.recent;
  // Until the user changes an input, the form follows the runs opened (the first of which may
  // arrive after the store was made), so it is prefilled from the last run, on its machine.
  let edited = false;
  let moreOpen = options.moreOptions?.read() ?? {};
  const restored = options.draft?.read() ?? null;
  // What is stored is what the store holds until the form changes it.
  let kept = restored === null ? null : storeDraft(restored);
  const keep = (snapshot: NewRunSnapshot): void => {
    const {answers, at, form} = snapshot.context;
    const draft = draftOf({answers, at, form});
    const text = draft === null ? null : storeDraft(draft);
    if (text === kept) return;
    kept = text;
    options.draft?.write(draft);
  };
  const spawn = (machine: HostKey, draft: Draft | null = null): Actor<typeof logic> => {
    const actor = createActor(logic, {input: {machine, recent, moreOpen, draft}, clock});
    actor.subscribe(snapshot => {
      const run = snapshot.context.started;
      if (snapshot.status === 'done' && run !== null) {
        edited = false;
        current = spawn(snapshot.context.form.machine);
        options.started({host: snapshot.context.form.machine, runId: run.runId});
      } else {
        keep(snapshot);
      }
      delivery.notify();
    });
    actor.start();
    return actor;
  };
  // A window that kept a draft is past the first change: the form is the user's, not the last run's.
  edited = restored !== null;
  let current = spawn(restored?.machine ?? initialMachine(recent), restored);
  const store: Store<NewRunSnapshot, NewRunEvent> = {
    getState: () => current.getSnapshot(),
    subscribe: delivery.subscribe,
    dispatch: event => {
      if (event.type === 'more_options') {
        // A choice of the page, not an input of the run: it neither edits the form nor is lost
        // when the form is replaced.
        current.send(event);
        moreOpen = current.getSnapshot().context.moreOpen;
        options.moreOptions?.write(moreOpen);
        return;
      }
      if (event.type === 'compute') {
        // What the machine has set up is read from its Settings data, not chosen: it arrives when
        // the data does, before or after the runs opened, and must not decide which form this is.
        current.send(event);
        return;
      }
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
  const completion = createFolderCompletion(bridge, options.schedule);
  return {store, completion, stop: () => current.stop()};
}
