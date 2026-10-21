/** Additive store.v1 read path. No desktop or legacy run-session coupling. */

export type {SkeletonCache} from './cache-role.js';
export {type RunTransport, skeletonLoaderMachineFor, startSkeletonLoader} from './loader.js';
export {
  type SkeletonLoaderInput,
  type SkeletonLoadFailure,
  skeletonLoaderMachine,
} from './loader-machine.js';
export {
  ingestSkeleton,
  ingestTail,
  observeRun,
  type RunLivenessHint,
  type SkeletonLoad,
} from './loading.js';
export {foldSkeleton, type Skeleton, seedSkeleton, seekSkeleton} from './model.js';
