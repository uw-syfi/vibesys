/**
 * One benchmark observation as the run fold records it, read from any of the wire's spellings
 * (`benchmark_result`, a `round_finished` summary, a finished benchmark gate). The fold and the
 * performance projection (`./performance`) share this one reading.
 */
import type {RunEvent} from '@vibesys/contracts';
import {type RoundKey, roundKeyFor, roundNumberFor} from './round-key.js';

/** One semantic benchmark observation folded into core state. */
export interface BenchmarkRecord {
  readonly sequence: number;
  readonly roundNumber: number | null;
  readonly roundKey: RoundKey | null;
  readonly metric: string;
  readonly value: number;
  readonly unit: string;
  /** Timestamp of the event the observation was read from. */
  readonly timestamp: string;
}

/** The benchmark observation `event` carries, or null when it carries none. */
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
      timestamp: event.timestamp,
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
      timestamp: event.timestamp,
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
    timestamp: event.timestamp,
  };
}
