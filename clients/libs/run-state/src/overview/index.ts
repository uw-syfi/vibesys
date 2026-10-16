/**
 * The run's overview: its objective and what measures it (the snapshot's `overview`, or the
 * `run_started` event that declared it), and the key numbers of the Overview and Work tabs as pure
 * selectors over the run trace's spans. Trajectories and counts are queries over spans: there is
 * no second record of the run's work.
 *
 * The selectors read the dynamic search's span kinds (`workstream`, `gate`); a run whose search
 * labels no spans yields empty trajectories and zero counts.
 */
import type {RunEvent, RunOverview, RunSnapshot, Span} from '@vibesys/contracts';

/** The overview a snapshot carries, or null before the run started. */
export function overviewFromSnapshot(snapshot: RunSnapshot): RunOverview | null {
  return snapshot.overview ?? null;
}

/** `current` with `events` folded: a `run_started` event that declares an overview replaces it. */
export function foldOverview(
  current: RunOverview | null,
  events: Iterable<RunEvent>,
): RunOverview | null {
  let overview = current;
  for (const event of events) {
    const data = event.data;
    if (event.type === 'run_started' && data?.kind === 'run_started' && data.overview) {
      overview = data.overview;
    }
  }
  return overview;
}

/** One trusted measurement of a candidate: a gate span and what it found. */
export interface GateReading {
  readonly span: string;
  /** The workstream span that measured it, when it is one. */
  readonly workstream: string | null;
  /** Run-clock seconds when the measurement ended (or started, while it runs). */
  readonly at: number;
  readonly metricValue: number | null;
  /** The candidate's value divided by the input's. */
  readonly vsBaseline: number | null;
  readonly verdict: string | null;
}

/** Every gate of the run in the order its measurement started: the performance trajectory. */
export function gateTrajectory(spans: readonly Span[]): GateReading[] {
  return spans.filter(span => span.label?.kind === 'gate').map(gateReading);
}

/** The best measured candidate that passed its gate, by the objective's direction. */
export function bestVsBaseline(
  spans: readonly Span[],
  direction: 'max' | 'min' | null | undefined,
): GateReading | null {
  const better =
    direction === 'min' ? (a: number, b: number) => a < b : (a: number, b: number) => a > b;
  let best: GateReading | null = null;
  for (const reading of gateTrajectory(spans)) {
    if (reading.verdict !== 'passed' || reading.metricValue === null) continue;
    if (best === null || better(reading.metricValue, best.metricValue ?? reading.metricValue)) {
      best = reading;
    }
  }
  return best;
}

/** How many workstreams the run has, by where each stands. */
export interface WorkstreamCounts {
  readonly total: number;
  /** Working on something of their own: an agent turn, a measurement, a task. */
  readonly live: number;
  /** Waiting on time, a latch or the stop (a slot, the input's measurement). */
  readonly waiting: number;
  readonly accepted: number;
  readonly rejected: number;
}

/** The workstreams of the run, counted by status and verdict. */
export function workstreamCounts(spans: readonly Span[]): WorkstreamCounts {
  let total = 0;
  let live = 0;
  let waiting = 0;
  let accepted = 0;
  let rejected = 0;
  for (const span of spans) {
    if (span.label?.kind !== 'workstream') continue;
    total += 1;
    if (span.status === 'running') live += 1;
    if (span.status === 'waiting' || span.status === 'pending') waiting += 1;
    if (span.label.verdict === 'accepted') accepted += 1;
    if (span.label.verdict === 'rejected') rejected += 1;
  }
  return {total, live, waiting, accepted, rejected};
}

function gateReading(span: Span): GateReading {
  const attrs = attributes(span);
  return {
    span: span.id,
    workstream: span.parent ?? null,
    at: span.ended_at ?? span.started_at,
    metricValue: numberOrNull(attrs['metric_value']),
    vsBaseline: numberOrNull(attrs['vs_baseline']),
    verdict: span.label?.verdict ?? null,
  };
}

function attributes(span: Span): Record<string, unknown> {
  const text = span.label?.attrs_json;
  if (!text) return {};
  const parsed: unknown = JSON.parse(text);
  return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : {};
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
