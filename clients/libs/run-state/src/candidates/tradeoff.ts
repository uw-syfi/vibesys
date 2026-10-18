/**
 * The trade-off of a run with two objectives: every settled candidate as a point on the two axes,
 * and where the input stands. A pure fold over the candidates (each gate span carries a reading
 * per axis and the input's readings), so a reader draws it at any time by the points' `at`.
 */
import type {Candidate, MeasuredReading} from './types.js';

/** One axis the run ranks candidates on, as its overview declares it. */
export interface ObjectiveAxis {
  readonly name: string;
  readonly direction: 'max' | 'min';
}

/** An axis with the unit the measurements reported for it (null when none did). */
export interface TradeoffAxis extends ObjectiveAxis {
  readonly unit: string | null;
}

/** One settled candidate's measurement on both axes. */
export interface TradeoffPoint {
  readonly id: string;
  readonly title: string;
  readonly x: number;
  readonly y: number;
  /** Run-clock seconds when the measurement ended. */
  readonly at: number;
  /** `passed` competes for the frontier; `failed` was turned down. */
  readonly outcome: 'passed' | 'failed';
}

/** The run's two-objective trade-off. */
export interface Tradeoff {
  readonly axes: readonly [TradeoffAxis, TradeoffAxis];
  /** Where the input stands on both axes; null while no gate has said. */
  readonly input: {readonly x: number; readonly y: number} | null;
  /** In the order measured. */
  readonly points: readonly TradeoffPoint[];
}

function readingOf(readings: readonly MeasuredReading[], name: string): number | null {
  return readings.find(reading => reading.name === name)?.value ?? null;
}

function unitOf(candidates: readonly Candidate[], name: string): string | null {
  for (const item of candidates) {
    const gate = item.measurement;
    if (gate === null) continue;
    const found = [...gate.readings, ...gate.inputReadings].find(
      reading => reading.name === name && reading.unit !== null,
    );
    if (found !== undefined) return found.unit;
  }
  return null;
}

/**
 * The trade-off of `candidates` on `objectives`, which must be exactly two axes (else null: a run
 * with one objective has its step line, and more than two is not drawn). A candidate is a point
 * once it has settled and its measurement read both axes; a running one is left out until it
 * passes or fails. Null while there is nothing to draw: no point yet.
 */
export function tradeoffOf(
  candidates: readonly Candidate[],
  objectives: readonly ObjectiveAxis[],
): Tradeoff | null {
  const [first, second] = objectives;
  if (objectives.length !== 2 || first === undefined || second === undefined) return null;
  const points: TradeoffPoint[] = [];
  let input: Tradeoff['input'] = null;
  for (const item of candidates) {
    const gate = item.measurement;
    if (gate === null) continue;
    const seenX = readingOf(gate.inputReadings, first.name);
    const seenY = readingOf(gate.inputReadings, second.name);
    if (input === null && seenX !== null && seenY !== null) input = {x: seenX, y: seenY};
    const x = readingOf(gate.readings, first.name);
    const y = readingOf(gate.readings, second.name);
    if (x === null || y === null || item.outcome === 'running') continue;
    points.push({id: item.id, title: item.title, x, y, at: gate.at, outcome: item.outcome});
  }
  if (points.length === 0) return null;
  points.sort((a, b) => a.at - b.at);
  return {
    axes: [
      {...first, unit: unitOf(candidates, first.name)},
      {...second, unit: unitOf(candidates, second.name)},
    ],
    input,
    points,
  };
}
