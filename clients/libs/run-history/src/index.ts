/**
 * The main process's store of ended runs' recorded events, shared by every window: read a page
 * through it and the host is asked once. See `store.ts` for what it holds, its bound and its
 * invalidation. A consumer builds one `RunHistoryStore` over a `HistorySource` (the host's
 * `runs.read_events`), answers every window's history read with `read`, and calls
 * `runsLive` / `invalidateHost` as the host's registry changes.
 */
export {
  DEFAULT_MAX_BYTES,
  type HistoryChunk,
  type HistoryKinds,
  type HistoryRead,
  type HistoryRunKey,
  type HistorySource,
  type RunHistoryOptions,
  type RunHistoryStats,
  RunHistoryStore,
} from './store.js';
