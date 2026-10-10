import type {HypothesisEntry, ProtocolResponse, RunEvent} from '@vibesys/backend-client';
import {type RoundKey, roundKeyFor, roundNumberFor} from './round-key.js';

/** Improvement direction recorded with hypothesis measurements. */
export type PerformanceDirection = NonNullable<HypothesisEntry['perf_direction']>;

/** One semantic benchmark observation folded into core state. */
export interface BenchmarkRecord {
  readonly sequence: number;
  readonly roundNumber: number | null;
  readonly roundKey: RoundKey | null;
  readonly metric: string;
  readonly value: number;
  readonly unit: string;
}

/** One plotted round, independent of any frontend chart geometry. */
export interface PerformancePoint {
  readonly round: number;
  readonly metric: string;
  readonly value: number;
  readonly unit: string;
  /** Recorded change from the comparison baseline, already normalized for every client. */
  readonly formattedDelta: string | null;
}

/** The complete client-neutral performance series. */
export interface PerformanceProjection {
  readonly points: readonly PerformancePoint[];
  /** Null when no hypothesis records a direction or recorded hypotheses disagree. */
  readonly direction: PerformanceDirection | null;
}

export interface PerformanceProjectionInput {
  readonly performance?: ProtocolResponse['performance'] | undefined;
  readonly events?: readonly RunEvent[] | undefined;
  readonly experiments?: readonly HypothesisEntry[] | undefined;
  /** Direction from the same official performance query as `performance`. */
  readonly objectiveDirection?: PerformanceDirection | null | undefined;
}

/**
 * Project every wire spelling of performance into one round-keyed series.
 *
 * Persisted performance records establish the base. Primary journal
 * measurements overwrite the same round in sequence order; a lossy legacy
 * `round_finished` summary fills only a round without a primary measurement.
 * That makes mixed old and new journals deterministic without double-counting
 * or discarding metric identity. The official performance query owns direction
 * when present; experiment records supply a consensus only to experiment-only
 * consumers. Hypotheses own recorded deltas.
 */
export function projectPerformance({
  performance = [],
  events = [],
  experiments = [],
  objectiveDirection = null,
}: PerformanceProjectionInput): PerformanceProjection {
  const deltas = performanceDeltas(experiments);
  const byRound = new Map<number, Omit<PerformancePoint, 'formattedDelta'>>();
  for (const record of performance ?? []) {
    byRound.set(record.round, {
      round: record.round,
      metric: record.perf_unit,
      value: record.perf_metric,
      unit: record.perf_unit,
    });
  }
  for (const event of events) {
    const record = benchmarkRecordFromEvent(event, event.sequence ?? 0);
    if (record === null || record.roundNumber === null) continue;
    if (event.data?.kind === 'round_finished' && byRound.has(record.roundNumber)) continue;
    byRound.set(record.roundNumber, {
      round: record.roundNumber,
      metric: record.metric,
      value: record.value,
      unit: record.unit,
    });
  }
  const points = [...byRound.values()]
    .sort((left, right) => left.round - right.round)
    .map(point => ({...point, formattedDelta: deltas.get(point.round) ?? null}));
  // The performance query is the official measurement view and wins over an
  // experiment log that may refresh independently. An experiment-only
  // consumer, such as its table header, still gets the recorded consensus.
  return {points, direction: objectiveDirection ?? recordedDirection(experiments)};
}

/** The canonical compact delta used by every client surface. */
export function formatPerformanceDelta(delta: number): string {
  const sign = delta > 0 ? '+' : '';
  return `${sign}${delta.toFixed(Math.abs(delta) >= 10 ? 0 : 1)}%`;
}

/** Whether this event means the backend performance records may have changed. */
export function recordsBenchmark(event: RunEvent): boolean {
  // Old servers can emit these envelope types without typed data while still
  // advancing their separately queried performance log. Keep refresh policy
  // broader than the in-memory fold without making a frontend sniff strings.
  return (
    event.type === 'benchmark_result' ||
    event.type === 'round_finished' ||
    benchmarkRecordFromEvent(event, event.sequence ?? 0) !== null
  );
}

/** Internal fold seam shared by CoreState and the public series projection. */
export function benchmarkRecordFromEvent(
  event: RunEvent,
  sequence: number,
): BenchmarkRecord | null {
  const data = event.data;
  const roundKey = roundKeyFor(event);
  if (data?.kind === 'benchmark_result') {
    return {
      sequence,
      roundNumber: roundNumberFor(roundKey),
      roundKey,
      metric: data.metric,
      value: data.value,
      unit: data.unit,
    };
  }
  if (data?.kind === 'round_finished' && typeof data.perf_metric === 'number') {
    const unit = data.perf_unit ?? 'performance';
    return {
      sequence,
      roundNumber: roundNumberFor(roundKey),
      roundKey,
      metric: unit,
      value: data.perf_metric,
      unit,
    };
  }
  // A completed benchmark gate carries the measurement `benchmark_result`
  // used to. A reused gate is a cache hit: it re-reports an earlier round's
  // number and must not append a phantom observation.
  if (
    data?.kind !== 'gate_finished' ||
    data.gate !== 'benchmark' ||
    event.status === 'failed' ||
    data.reused === true ||
    data.metric == null ||
    data.value == null
  ) {
    return null;
  }
  return {
    sequence,
    roundNumber: roundNumberFor(roundKey),
    roundKey,
    metric: data.metric,
    value: data.value,
    unit: data.unit ?? data.metric,
  };
}

function performanceDeltas(experiments: readonly HypothesisEntry[]): ReadonlyMap<number, string> {
  const deltas = new Map<number, string>();
  for (const experiment of experiments) {
    for (const round of experiment.rounds ?? []) {
      if (typeof round.perf_delta_pct === 'number') {
        deltas.set(round.round, formatPerformanceDelta(round.perf_delta_pct));
      }
    }
    if (typeof experiment.perf_delta_pct === 'number' && !deltas.has(experiment.last_round)) {
      deltas.set(experiment.last_round, formatPerformanceDelta(experiment.perf_delta_pct));
    }
  }
  return deltas;
}

function recordedDirection(experiments: readonly HypothesisEntry[]): PerformanceDirection | null {
  let direction: PerformanceDirection | null = null;
  for (const experiment of experiments) {
    const candidate = experiment.perf_direction ?? null;
    if (candidate === null) continue;
    if (direction === null) direction = candidate;
    else if (direction !== candidate) return null;
  }
  return direction;
}
