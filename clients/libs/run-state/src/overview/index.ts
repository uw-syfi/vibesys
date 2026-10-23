/**
 * The run's overview: its objective and what measures it (the snapshot's `overview`, or the
 * `run_started` event that declared it), and the key numbers of the Overview and Work tabs as pure
 * selectors over the run trace's spans. Trajectories and counts are queries over spans: there is
 * no second record of the run's work.
 *
 * The selectors read the span kinds every hypothesis search labels (`workstream`, `gate`,
 * `adoption`; the vocabulary is `vibesys.search_spans`); a run whose search labels no spans yields
 * empty trajectories and zero counts.
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

/**
 * What the run adopted: the search's own account of its winner, the `adoption` span (the
 * operation that adopts it) with its verdict.
 */
export interface Adoption {
  readonly span: string;
  /** The adopted revision; null when the input itself stands (no candidate beat it). */
  readonly candidate: string | null;
  readonly metricValue: number | null;
  /** The adopted candidate's value divided by the input's. */
  readonly vsBaseline: number | null;
  /** `adopted` or `not_adopted` once core has answered; null while it is adopting. */
  readonly verdict: string | null;
}

/** The run's latest adoption, or null when none has begun (the run is still searching). */
export function adoption(spans: readonly Span[]): Adoption | null {
  const found = spans.filter(span => span.label?.kind === 'adoption').at(-1);
  if (found === undefined) return null;
  const attrs = attributes(found);
  const candidate = attrs['candidate'];
  return {
    span: found.id,
    candidate: typeof candidate === 'string' && candidate !== '' ? candidate : null,
    metricValue: numberOrNull(attrs['metric_value']),
    vsBaseline: numberOrNull(attrs['vs_baseline']),
    verdict: found.label?.verdict ?? null,
  };
}

/** How many workstreams the run has, by where each stands. */
export interface WorkstreamCounts {
  readonly total: number;
  /** Working on something of their own: an agent turn, a measurement, a task. */
  readonly live: number;
  /** Waiting on time, a latch or the stop (a slot, the input's measurement). */
  readonly waiting: number;
  readonly passed: number;
  readonly failed: number;
}

/** The workstreams of the run, counted by status and verdict (passed or failed). */
export function workstreamCounts(spans: readonly Span[]): WorkstreamCounts {
  let total = 0;
  let live = 0;
  let waiting = 0;
  let passed = 0;
  let failed = 0;
  for (const span of spans) {
    if (span.label?.kind !== 'workstream') continue;
    total += 1;
    if (span.status === 'running') live += 1;
    if (span.status === 'waiting' || span.status === 'pending') waiting += 1;
    if (span.label.verdict === 'passed') passed += 1;
    if (span.label.verdict === 'failed') failed += 1;
  }
  return {total, live, waiting, passed, failed};
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
