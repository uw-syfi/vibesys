/**
 * `@vibesys/run-state/performance`: the client-neutral performance series, projected from every
 * wire spelling of a benchmark measurement.
 */
export type {BenchmarkRecord} from '../benchmark.js';
export {
  formatPerformanceDelta,
  type PerformanceDirection,
  type PerformancePoint,
  type PerformanceProjection,
  type PerformanceProjectionInput,
  projectPerformance,
  recordsBenchmark,
} from './projection.js';
