import type {RunEvent} from '@vibesys/backend-client';
import {type CoreState, initialCoreState, reduceEventBatch} from '@vibesys/core-state';

export interface CoreStateStore {
  getState(): CoreState;
  subscribe(listener: () => void): () => void;
  append(events: readonly RunEvent[]): void;
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
  };
}
