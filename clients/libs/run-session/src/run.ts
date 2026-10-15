/**
 * The run fold: the run's `CoreState`, advanced by `@vibesys/run-state`'s reducers. A snapshot, a batch the
 * stream reconciler placed, or a replayed run's events each fold in one step that asks for no I/O.
 * A data fold, so a plain reducer on `@vibesys/store` (docs/contributing/clients-architecture.md,
 * "State"); this is the package's one fold module.
 */

import type {EventBatchMessage, RunEvent, RunSnapshot} from '@vibesys/contracts';
import {
  type ActiveExecutionCheckpointFold,
  type CoreState,
  type IterationNavigationState,
  initialCoreState,
  initialIterationNavigation,
  reduceEventBatch,
  reduceEventRebootstrap,
  reduceSnapshot,
} from '@vibesys/run-state';
import {createStore, only, type Step, type Store} from '@vibesys/store';
import type {BatchReconciliation} from '@vibesys/transport';

export type RunAction =
  /** A replayed run's events, in order. */
  | {readonly type: 'events'; readonly events: readonly RunEvent[]}
  | {readonly type: 'snapshot'; readonly snapshot: RunSnapshot}
  /** One stream batch and how the stream reconciler placed it. */
  | {
      readonly type: 'batch';
      readonly message: EventBatchMessage;
      readonly reconciliation: BatchReconciliation;
      /**
       * The private transaction the persistent stream derived while assembling
       * a fragmented execution checkpoint.
       */
      readonly activeExecutionCheckpoint?: ActiveExecutionCheckpointFold;
    };

export function runStep(state: CoreState, action: RunAction): Step<CoreState, never> {
  switch (action.type) {
    case 'events':
      return only(action.events.length === 0 ? state : reduceEventBatch(state, action.events));
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
          action.activeExecutionCheckpoint,
        ),
      );
    }
  }
}

/** A complete navigation result belongs to another run than the combined store's run fold. */
export interface IterationRunMismatch {
  readonly kind: 'run-mismatch';
  readonly expectedRunId: string;
  readonly receivedRunId: string;
}

/** What publishing a complete navigation traversal did. */
export type IterationPublicationOutcome =
  | {readonly kind: 'published'}
  | {readonly kind: 'deferred'}
  | IterationRunMismatch;

/** The run fold and policy-navigation stores exposed as one identity-safe render subscription. */
export interface RunStore extends Omit<Store<CoreState, RunAction>, 'dispatch'> {
  getIterations(): IterationNavigationState;
  /** Fold run data and report a navigation result rejected when this action established identity. */
  dispatch(action: RunAction): IterationRunMismatch | null;
  /** Publish, defer until run identity is known, or reject a foreign traversal. */
  publishIterations(navigation: IterationNavigationState): IterationPublicationOutcome;
}

interface RunViewState {
  readonly run: CoreState;
  readonly iterations: IterationNavigationState;
  /** A complete traversal that arrived before the run fold established its identity. */
  readonly pendingIterations: IterationNavigationState | null;
}

interface RunViewTransition {
  readonly state: RunViewState;
  readonly mismatch: IterationRunMismatch | null;
}

function mismatch(expectedRunId: string, receivedRunId: string): IterationRunMismatch {
  return {kind: 'run-mismatch', expectedRunId, receivedRunId};
}

/** Fold run data, then reconcile every visible or deferred traversal against its new identity. */
function foldRunView(state: RunViewState, action: RunAction): RunViewTransition {
  const run = runStep(state.run, action).state;
  let iterations = state.iterations;
  let pendingIterations = state.pendingIterations;
  let rejected: IterationRunMismatch | null = null;

  if (pendingIterations !== null && run.runId !== null) {
    if (pendingIterations.runId === run.runId) iterations = pendingIterations;
    else if (pendingIterations.runId !== null) {
      rejected = mismatch(run.runId, pendingIterations.runId);
    }
    pendingIterations = null;
  }
  // A fresh bootstrap can temporarily make the run identity unknown. Keep the last proven view
  // while it is unknown; only a newly established, different identity disproves that view.
  if (run.runId !== null && iterations.runId !== null && iterations.runId !== run.runId) {
    iterations = initialIterationNavigation();
  }

  const unchanged =
    run === state.run &&
    iterations === state.iterations &&
    pendingIterations === state.pendingIterations;
  return {
    state: unchanged ? state : {run, iterations, pendingIterations},
    mismatch: rejected,
  };
}

/** Reconcile one complete traversal without ever putting a foreign identity on the render path. */
function publishRunIterations(
  state: RunViewState,
  navigation: IterationNavigationState,
): {readonly state: RunViewState; readonly outcome: IterationPublicationOutcome} {
  const runId = state.run.runId;
  const navigationRunId = navigation.runId;
  if (navigationRunId === null || navigationRunId === runId) {
    const unchanged = state.iterations === navigation && state.pendingIterations === null;
    return {
      state: unchanged ? state : {...state, iterations: navigation, pendingIterations: null},
      outcome: {kind: 'published'},
    };
  }
  if (runId === null) {
    return {
      state:
        state.pendingIterations === navigation ? state : {...state, pendingIterations: navigation},
      outcome: {kind: 'deferred'},
    };
  }
  return {state, outcome: mismatch(runId, navigationRunId)};
}

export function createRunStore(): RunStore {
  type Replace = {readonly state: RunViewState};
  const store = createStore(
    (_state: RunViewState, action: Replace) => only(action.state),
    {
      run: initialCoreState(),
      iterations: initialIterationNavigation(),
      pendingIterations: null,
    },
    () => undefined,
  );
  return {
    getState: () => store.getState().run,
    getIterations: () => store.getState().iterations,
    subscribe: store.subscribe,
    dispatch: action => {
      const next = foldRunView(store.getState(), action);
      if (next.state !== store.getState()) store.dispatch({state: next.state});
      return next.mismatch;
    },
    publishIterations: navigation => {
      const next = publishRunIterations(store.getState(), navigation);
      if (next.state !== store.getState()) store.dispatch({state: next.state});
      return next.outcome;
    },
  };
}
