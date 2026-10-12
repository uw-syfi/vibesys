/**
 * The run fold: the run's `CoreState`, advanced by `@vibesys/run-state`'s reducers. A snapshot, a batch the
 * stream reconciler placed, or a replayed run's events each fold in one step that asks for no I/O.
 * A data fold, so a plain reducer on `@vibesys/store` (docs/contributing/clients-architecture.md,
 * "State"); this is the package's one fold module.
 */

import type {EventBatchMessage, RunEvent, RunSnapshot} from '@vibesys/contracts';
import {
  type CoreState,
  initialCoreState,
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
        ),
      );
    }
  }
}

/** The run fold's store: what the run page renders as the run. */
export type RunStore = Store<CoreState, RunAction>;

export function createRunStore(): RunStore {
  return createStore(runStep, initialCoreState(), () => undefined);
}
