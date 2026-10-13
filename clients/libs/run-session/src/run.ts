/**
 * The run fold: the run's `CoreState`, advanced by `@vibesys/run-state`'s reducers. A snapshot, a batch the
 * stream reconciler placed, or a replayed run's events each fold in one step that asks for no I/O.
 * A data fold, so a plain reducer on `@vibesys/store` (docs/contributing/clients-architecture.md,
 * "State"); this is the package's one fold module.
 */

import type {EventBatchMessage, RunEvent, RunSnapshot} from '@vibesys/contracts';
import {
  type CoreState,
  type IterationNavigationState,
  initialCoreState,
  initialIterationNavigation,
  reduceEventBatch,
  reduceEventRebootstrap,
  reduceResponseEvents,
  reduceSnapshot,
} from '@vibesys/run-state';
import {createStore, only, type Step, type Store} from '@vibesys/store';
import type {BatchReconciliation} from '@vibesys/transport';

export type RunAction =
  /** A replayed run's events, in order. */
  | {readonly type: 'events'; readonly events: readonly RunEvent[]}
  | {readonly type: 'snapshot'; readonly snapshot: RunSnapshot}
  /** Request-response events must not claim the subscription cursor advanced. */
  | {readonly type: 'response-events'; readonly events: readonly RunEvent[]}
  /** One stream batch and how the stream reconciler placed it. */
  | {
      readonly type: 'batch';
      readonly message: EventBatchMessage;
      readonly reconciliation: BatchReconciliation;
    };

export function runStep(state: CoreState, action: RunAction): Step<CoreState, never> {
  switch (action.type) {
    case 'events':
      return only(action.events.length === 0 ? state : reduceEventBatch(state, action.events));
    case 'response-events':
      return only(action.events.length === 0 ? state : reduceResponseEvents(state, action.events));
    case 'snapshot':
      return only(reduceSnapshot(state, action.snapshot));
    case 'batch': {
      const {message, reconciliation} = action;
      const fold =
        reconciliation.kind === 'rebootstrap' ? reduceEventRebootstrap : reduceEventBatch;
      return only(
        fold(
          state,
          message.events,
          message.active_executions,
          message.through_sequence,
          reconciliation.historyFloor,
        ),
      );
    }
  }
}

/** The run fold and policy-navigation stores exposed as one render subscription. */
export interface RunStore extends Store<CoreState, RunAction> {
  getIterations(): IterationNavigationState;
  publishIterations(navigation: IterationNavigationState): void;
}

interface RunViewState {
  readonly run: CoreState;
  readonly iterations: IterationNavigationState;
}

type RunViewAction =
  | {readonly type: 'run'; readonly action: RunAction}
  | {readonly type: 'iterations'; readonly navigation: IterationNavigationState};

export function createRunStore(): RunStore {
  const store = createStore(
    (state: RunViewState, action: RunViewAction) => {
      if (action.type === 'iterations') {
        if (state.iterations === action.navigation) return only(state);
        return only({...state, iterations: action.navigation});
      }
      const next = runStep(state.run, action.action).state;
      return only(next === state.run ? state : {...state, run: next});
    },
    {run: initialCoreState(), iterations: initialIterationNavigation()},
    () => undefined,
  );
  return {
    getState: () => store.getState().run,
    getIterations: () => store.getState().iterations,
    subscribe: store.subscribe,
    dispatch: action => store.dispatch({type: 'run', action}),
    publishIterations: navigation => store.dispatch({type: 'iterations', navigation}),
  };
}
