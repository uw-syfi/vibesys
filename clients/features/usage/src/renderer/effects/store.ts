/**
 * The Usage page's shell: its machine, with the question provided by `loadUsage`, seen as a store
 * the screen renders. The machine starts when the screen first subscribes and asks again each time
 * the screen is shown after it was left, so the page never opens on figures from an earlier visit
 * without saying it is reading them again. A machine whose connection comes up while the page is
 * shown asks again, so figures that left it out, or a question that failed because it was
 * unreachable, are read afresh without the user pressing Retry.
 */
import {type HostListing, hostsComeUp} from '@vibesys/hosts';
import {actorStore, clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import type {Store} from '@vibesys/store';
import {createActor, fromPromise} from 'xstate';
import {
  type UsageActions,
  type UsageContext,
  type UsagePhase,
  type UsageScreenState,
  type UsageWindowChoice,
  usageMachine,
  type WindowClock,
} from '../../state/index.js';
import {loadUsage, type UsageBridge} from './load.js';

export interface UsageStoreOptions {
  readonly schedule: ScheduleTimeout;
  /** The clock a question reads: now, and the local midnight before it. */
  readonly clock: () => WindowClock;
  /** The window the page opens on. */
  readonly window?: UsageWindowChoice;
}

const PHASES: readonly string[] = ['loading', 'ready', 'unsupported', 'failed'];

function phaseOf(value: unknown): UsagePhase {
  if (typeof value === 'string' && PHASES.includes(value)) return value as UsagePhase;
  throw new Error(`the usage machine is in an unknown state: ${JSON.stringify(value)}`);
}

/**
 * The Usage page's store over `bridge`. `stop` ends the machine and the question it is asking.
 */
export function createUsageStore(
  bridge: UsageBridge,
  options: UsageStoreOptions,
): {
  readonly store: Store<UsageScreenState, never>;
  readonly actions: UsageActions;
  readonly stop: () => void;
} {
  const logic = usageMachine.provide({
    actors: {
      load: fromPromise(({input, signal}) => loadUsage(bridge, input.span, signal)),
    },
  });
  const actor = createActor(logic, {
    input: {window: options.window ?? 'week', clock: options.clock()},
    clock: clockFrom(options.schedule),
  });
  // A snapshot's screen state is the same object while the machine's state and context are.
  let last: {
    readonly value: unknown;
    readonly context: UsageContext;
    readonly screen: UsageScreenState;
  } | null = null;
  const screenOf = (snapshot: {readonly value: unknown; readonly context: UsageContext}) => {
    if (last !== null && last.value === snapshot.value && last.context === snapshot.context) {
      return last.screen;
    }
    const {window, view, problem} = snapshot.context;
    const screen: UsageScreenState = {window, phase: phaseOf(snapshot.value), view, problem};
    last = {value: snapshot.value, context: snapshot.context, screen};
    return screen;
  };
  const inner = actorStore(actor, screenOf);
  let started = false;
  let watching = 0;
  let listings: readonly HostListing[] = [];
  bridge.onListing(event => {
    const reachable = hostsComeUp(listings, [event.value]).length > 0;
    listings = [...listings.filter(each => each.host !== event.value.host), event.value];
    // A page that is not shown asks again when it is.
    if (reachable && watching > 0) actor.send({type: 'refresh', clock: options.clock()});
  });
  const store: Store<UsageScreenState, never> = {
    getState: inner.getState,
    dispatch: () => undefined,
    subscribe: listener => {
      const unsubscribe = inner.subscribe(listener);
      watching += 1;
      if (!started) {
        started = true;
        actor.start();
      } else if (watching === 1) {
        actor.send({type: 'refresh', clock: options.clock()});
      }
      return () => {
        watching -= 1;
        unsubscribe();
      };
    },
  };
  return {
    store,
    actions: {
      choose: window => actor.send({type: 'choose', window, clock: options.clock()}),
      refresh: () => actor.send({type: 'refresh', clock: options.clock()}),
    },
    stop: () => actor.stop(),
  };
}
