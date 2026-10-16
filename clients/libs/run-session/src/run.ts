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
  foldEventBatch,
  foldEventRebootstrap,
  foldResponseEvents,
  foldSnapshot,
  type IterationNavigationState,
  initialCoreState,
  initialIterationNavigation,
  type RunIdentityRejection,
  type SnapshotFoldDisposition,
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
      /**
       * The private transaction the persistent stream derived while assembling
       * a fragmented execution checkpoint.
       */
      readonly activeExecutionCheckpoint?: ActiveExecutionCheckpointFold;
    };

type RunActionEvents = Extract<RunAction, {readonly events: unknown}>['events'];

/** Trace data accepted by the authoritative run fold, plus its stream-store disposition. */
export type RunTracePublication =
  | {readonly kind: 'snapshot'; readonly snapshot: RunSnapshot}
  | {readonly kind: 'events'; readonly events: RunActionEvents}
  | {
      readonly kind: 'rebootstrap';
      readonly base: 'preserve' | 'reset';
      readonly events: RunActionEvents;
    };

interface RunFoldTransition {
  readonly state: CoreState;
  /** This action preserves the complete combined run view, including private projection facts. */
  readonly noOp: boolean;
  readonly snapshot: SnapshotFoldDisposition | null;
  readonly acceptedEvents: readonly RunEvent[];
  readonly rejection: RunIdentityRejection | null;
}

/** Fold once and retain every secondary-projection fact from that same authoritative transition. */
function foldRun(state: CoreState, action: RunAction): RunFoldTransition {
  switch (action.type) {
    case 'events': {
      if (action.events.length === 0) return unchangedRun(state);
      const folded = foldEventBatch(state, action.events);
      return {...folded, noOp: false, snapshot: null};
    }
    case 'response-events': {
      if (action.events.length === 0) return unchangedRun(state);
      const folded = foldResponseEvents(state, action.events);
      return {...folded, noOp: false, snapshot: null};
    }
    case 'snapshot': {
      const folded = foldSnapshot(state, action.snapshot);
      return {
        state: folded.state,
        noOp: false,
        snapshot: folded.disposition,
        acceptedEvents: [],
        rejection: folded.rejection,
      };
    }
    case 'batch': {
      const {message, reconciliation} = action;
      const fold = reconciliation.kind === 'rebootstrap' ? foldEventRebootstrap : foldEventBatch;
      const folded = fold(
        state,
        message.events,
        message.active_executions,
        message.through_sequence,
        reconciliation.historyFloor,
        action.activeExecutionCheckpoint,
      );
      return {...folded, noOp: false, snapshot: null};
    }
  }
}

function unchangedRun(state: CoreState): RunFoldTransition {
  return {state, noOp: true, snapshot: null, acceptedEvents: [], rejection: null};
}

export function runStep(state: CoreState, action: RunAction): Step<CoreState, never> {
  return only(foldRun(state, action).state);
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
  /** Fold once and return every secondary publication fact from that transition. */
  dispatchWithOutcome(action: RunAction): RunDispatchOutcome;
  /** Publish, defer until run identity is known, or reject a foreign traversal. */
  publishIterations(navigation: IterationNavigationState): IterationPublicationOutcome;
}

/** The observable disposition of one authoritative run-store transition. */
export interface RunDispatchOutcome {
  readonly iterationMismatch: IterationRunMismatch | null;
  /** Snapshot liveness is independent from trace publication. */
  readonly snapshot: SnapshotFoldDisposition | null;
  readonly trace: RunTracePublication | null;
  readonly runIdentityRejection: RunIdentityRejection | null;
}

interface TracePublicationState {
  /** Event store that last established the trace suffix. */
  readonly storeId: string | null;
  /** Identity explicitly accepted for the trace's current base and suffix. */
  readonly runId: string | null;
  /** Newest snapshot accepted for the historical base, independent of the event cursor. */
  readonly snapshotSequence: number | null;
}

interface RunViewState {
  readonly run: CoreState;
  readonly iterations: IterationNavigationState;
  /** A complete traversal that arrived before the run fold established its identity. */
  readonly pendingIterations: IterationNavigationState | null;
  readonly tracePublication: TracePublicationState;
}

interface RunViewTransition {
  readonly state: RunViewState;
  readonly mismatch: IterationRunMismatch | null;
  readonly snapshot: SnapshotFoldDisposition | null;
  readonly trace: RunTracePublication | null;
  readonly runIdentityRejection: RunIdentityRejection | null;
}

function mismatch(expectedRunId: string, receivedRunId: string): IterationRunMismatch {
  return {kind: 'run-mismatch', expectedRunId, receivedRunId};
}

function publishTrace(
  trace: TracePublicationState,
  action: RunAction,
  folded: RunFoldTransition,
): {readonly state: TracePublicationState; readonly publication: RunTracePublication | null} {
  if (action.type === 'snapshot') {
    return publishTraceSnapshot(trace, action.snapshot, folded.rejection);
  }
  if (action.type === 'batch') {
    return publishTraceBatch(trace, action, folded);
  }
  return {
    state: {...trace, runId: folded.state.runId},
    publication:
      folded.acceptedEvents.length === 0 ? null : {kind: 'events', events: folded.acceptedEvents},
  };
}

function publishTraceSnapshot(
  trace: TracePublicationState,
  snapshot: RunSnapshot,
  rejection: RunIdentityRejection | null,
): {readonly state: TracePublicationState; readonly publication: RunTracePublication | null} {
  if (
    rejection !== null ||
    (trace.snapshotSequence !== null && snapshot.sequence < trace.snapshotSequence)
  ) {
    return {state: trace, publication: null};
  }
  return {
    state: {...trace, runId: snapshot.run_id, snapshotSequence: snapshot.sequence},
    publication: {kind: 'snapshot', snapshot},
  };
}

function publishTraceBatch(
  trace: TracePublicationState,
  action: Extract<RunAction, {readonly type: 'batch'}>,
  folded: RunFoldTransition,
): {readonly state: TracePublicationState; readonly publication: RunTracePublication | null} {
  const storeId = action.message.store_id ?? '';
  if (action.reconciliation.kind !== 'rebootstrap') {
    return {
      state: {...trace, storeId, runId: folded.state.runId},
      publication:
        folded.acceptedEvents.length === 0 ? null : {kind: 'events', events: folded.acceptedEvents},
    };
  }
  const provesSameRun =
    trace.runId !== null && folded.rejection === null && folded.state.runId === trace.runId;
  const preserve = trace.storeId !== null && trace.storeId === storeId && provesSameRun;
  return {
    state: {
      storeId,
      runId: folded.rejection === null ? folded.state.runId : null,
      snapshotSequence: preserve ? trace.snapshotSequence : null,
    },
    publication: {
      kind: 'rebootstrap',
      base: preserve ? 'preserve' : 'reset',
      events: folded.acceptedEvents,
    },
  };
}

/** Fold run data, then reconcile every visible or deferred traversal against its new identity. */
function foldRunView(state: RunViewState, action: RunAction): RunViewTransition {
  const folded = foldRun(state.run, action);
  if (folded.noOp) {
    return {
      state,
      mismatch: null,
      snapshot: null,
      trace: null,
      runIdentityRejection: null,
    };
  }
  const run = folded.state;
  const trace = publishTrace(state.tracePublication, action, folded);
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
    pendingIterations === state.pendingIterations &&
    trace.state === state.tracePublication;
  return {
    state: unchanged ? state : {run, iterations, pendingIterations, tracePublication: trace.state},
    mismatch: rejected,
    snapshot: folded.snapshot,
    trace: trace.publication,
    runIdentityRejection: folded.rejection,
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
      tracePublication: {storeId: null, runId: null, snapshotSequence: null},
    },
    () => undefined,
  );
  const dispatchWithOutcome = (action: RunAction): RunDispatchOutcome => {
    const next = foldRunView(store.getState(), action);
    if (next.state !== store.getState()) store.dispatch({state: next.state});
    return {
      iterationMismatch: next.mismatch,
      snapshot: next.snapshot,
      trace: next.trace,
      runIdentityRejection: next.runIdentityRejection,
    };
  };
  return {
    getState: () => store.getState().run,
    getIterations: () => store.getState().iterations,
    subscribe: store.subscribe,
    dispatch: action => dispatchWithOutcome(action).iterationMismatch,
    dispatchWithOutcome,
    publishIterations: navigation => {
      const next = publishRunIterations(store.getState(), navigation);
      if (next.state !== store.getState()) store.dispatch({state: next.state});
      return next.outcome;
    },
  };
}
