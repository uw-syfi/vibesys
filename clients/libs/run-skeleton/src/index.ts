/** Additive store.v1 read path. No desktop or legacy run-session coupling. */

export type {SkeletonCache} from './cache-role.js';
export {DescriptorCache, type DescriptorLease, DescriptorsUnknown} from './descriptors.js';
export {type RecordsFanout, recordsFanout} from './fanout.js';
export {
  FAILURE_PAGES,
  type RecordsListener,
  type RunTransport,
  skeletonLoaderMachineFor,
  startSkeletonLoader,
} from './loader.js';
export {
  MAX_ATTEMPTS,
  RETRY_CAP_MS,
  retryDelay,
  type SkeletonLoaderInput,
  type SkeletonLoadFailure,
  skeletonLoaderMachine,
} from './loader-machine.js';
export {
  epochHasEnded,
  ingestSkeleton,
  ingestTail,
  type LoadProgress,
  loadProgress,
  observeRun,
  progressFraction,
  RECORD_WINDOW,
  type RunLivenessHint,
  type SkeletonLoad,
} from './loading.js';
export {
  foldSkeleton,
  type Skeleton,
  seedSkeleton,
  seekSkeleton,
  transcriptStreamFor,
} from './model.js';
export type {RunLivenessFeed, RunLivenessState} from './observation.js';
export {RECORD_FACTS} from './record-facts.js';
export {type LoadRefusal, refusalOf} from './status.js';
