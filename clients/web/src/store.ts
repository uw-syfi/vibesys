import type {
  BatchReconciliation,
  EventBatchMessage,
  RunEvent,
  RunSnapshot,
} from '@vibesys/backend-client';
import {
  type ActiveExecutionCheckpoint,
  type CoreState,
  initialCoreState,
  reduceEventBatch,
  reduceEventRebootstrap,
  reduceSnapshot,
} from '@vibesys/core-state';

export interface CoreStateStore {
  getState(): CoreState;
  subscribe(listener: () => void): () => void;
  append(events: readonly RunEvent[]): void;
  applySnapshot(snapshot: RunSnapshot): void;
  applyBatch(message: EventBatchMessage, reconciliation: BatchReconciliation): void;
}

export function createCoreStateStore(seed: CoreState = initialCoreState()): CoreStateStore {
  let state = seed;
  const listeners = new Set<() => void>();
  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    append(events) {
      if (events.length === 0) return;
      state = reduceEventBatch(state, events);
      for (const listener of listeners) listener();
    },
    applySnapshot(snapshot) {
      state = reduceSnapshot(state, snapshot);
      for (const listener of listeners) listener();
    },
    applyBatch(message, reconciliation) {
      const events = message.events;
      const activeExecutions: ActiveExecutionCheckpoint = message.active_executions ?? [];
      state =
        reconciliation.kind === 'rebootstrap'
          ? reduceEventRebootstrap(
              state,
              events,
              activeExecutions,
              message.through_sequence,
              reconciliation.historyFloor,
            )
          : reduceEventBatch(
              state,
              events,
              activeExecutions,
              message.through_sequence,
              reconciliation.historyFloor,
            );
      for (const listener of listeners) listener();
    },
  };
}
