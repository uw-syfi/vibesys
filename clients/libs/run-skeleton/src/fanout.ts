import type {RecordsListener} from './loader.js';

/** Hands the records of a run's one reader to every consumer; none opens a read of its own. */
export interface RecordsFanout {
  readonly publish: RecordsListener;
  subscribe(listener: RecordsListener): () => void;
}

export function recordsFanout(): RecordsFanout {
  const listeners = new Set<RecordsListener>();
  return {
    publish: records => {
      for (const listener of [...listeners]) listener(records);
    },
    subscribe: listener => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
