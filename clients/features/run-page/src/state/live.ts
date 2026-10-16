/**
 * The run's own data on the Run screen: the link the window holds to a live run (attach, then the
 * session's connection), what the session reports, the run fold (`CoreState`), and the trace's
 * span events (and the `run_started` event that declares the run's overview) in sequence order,
 * so every view folds the trace and the overview at the sequence it shows (`traceAt`, `overviewAt`):
 * the live end, or a past sequence the scrubber chose. History (an ended run's
 * recorded events, `runs.read_events`) folds into the same slice, so the tabs have one source.
 */
import type {RunEvent, RunOverview, RunSnapshot} from '@vibesys/contracts';
import type {CoreState} from '@vibesys/run-state';
import {foldOverview, overviewFromSnapshot} from '@vibesys/run-state/overview';
import {
  EMPTY_TRACE,
  foldTrace,
  type RunTrace,
  traceFromSnapshot,
  traceSpans,
} from '@vibesys/run-state/trace';
import {latestTime} from './span-reading.js';

/** The window's hold on the live run: none, being attached under a token, attached, or refused. */
export type RunLink =
  | {readonly kind: 'none'}
  | {readonly kind: 'attaching'; readonly token: number; readonly instance: string}
  | {readonly kind: 'attached'; readonly instance: string}
  | {readonly kind: 'failed'; readonly instance: string; readonly message: string};

/** What the live session reports, in the screen's terms. */
export interface SessionView {
  /** A valid snapshot has been folded: the run data on screen is the run's. */
  readonly hasSnapshot: boolean;
  /** The event stream is live (not reconnecting or stale). */
  readonly streaming: boolean;
  /** A command sent now can be delivered. */
  readonly controls: boolean;
  readonly error: string | null;
}

/**
 * The trace as a base (a snapshot's spans, or nothing) plus the span events after it, in sequence
 * order, so it can be folded at any sequence from the base's on.
 */
export interface TraceLog {
  readonly base: RunTrace;
  /** The overview at the base (a snapshot's), or null before one declared it. */
  readonly baseOverview: RunOverview | null;
  readonly events: readonly RunEvent[];
}

export const EMPTY_TRACE_LOG: TraceLog = {base: EMPTY_TRACE, baseOverview: null, events: []};

/** The events the slice keeps: the trace's span events and the overview's declaration. */
const SPAN_EVENTS: ReadonlySet<string> = new Set([
  'span_started',
  'span_updated',
  'span_ended',
  'run_started',
]);

/** What the session folded into its run, as the trace slice needs it. */
export type RunData =
  | {readonly kind: 'snapshot'; readonly snapshot: RunSnapshot}
  | {readonly kind: 'events'; readonly events: readonly RunEvent[]}
  /** The stream replaced its replay; a proven new store also replaces the trace base. */
  | {
      readonly kind: 'rebootstrap';
      readonly base: 'preserve' | 'reset';
      readonly events: readonly RunEvent[];
    };

function lastSequence(log: TraceLog): number {
  return log.events.at(-1)?.sequence ?? log.base.sequence;
}

/** `log` with the span events of `events` that come after it appended, in order. */
function appended(log: TraceLog, events: readonly RunEvent[]): TraceLog {
  let last = lastSequence(log);
  const added: RunEvent[] = [];
  for (const event of events) {
    const at = event.sequence ?? 0;
    if (at <= last) continue;
    last = at;
    if (SPAN_EVENTS.has(event.type)) added.push(event);
  }
  return added.length === 0 ? log : {...log, events: [...log.events, ...added]};
}

/** The trace slice after `data`. */
export function traceStep(log: TraceLog, data: RunData): TraceLog {
  switch (data.kind) {
    case 'snapshot': {
      const base = traceFromSnapshot(data.snapshot);
      const after = log.events.filter(event => (event.sequence ?? 0) > base.sequence);
      return {base, baseOverview: overviewFromSnapshot(data.snapshot), events: after};
    }
    case 'events':
      return appended(log, data.events);
    case 'rebootstrap':
      return appended(
        data.base === 'preserve' ? {...log, events: []} : EMPTY_TRACE_LOG,
        data.events,
      );
  }
}

/** The trace at sequence `at` (at least the base's), or at the live end when `at` is null. */
function eventsAt(log: TraceLog, at: number | null): readonly RunEvent[] {
  return at === null ? log.events : log.events.filter(event => (event.sequence ?? 0) <= at);
}

export function traceAt(log: TraceLog, at: number | null): RunTrace {
  return foldTrace(log.base, eventsAt(log, at));
}

/** The run's overview at sequence `at`, or at the live end when `at` is null. */
export function overviewAt(log: TraceLog, at: number | null): RunOverview | null {
  return foldOverview(log.baseOverview, eventsAt(log, at));
}

/**
 * Where the run clock stood when this window last heard the run's spans change, and the window's
 * clock then (epoch ms). The run's own clock is read only from its spans, which change when a step
 * does, so a step that runs for minutes shows no time passing; this lets the page carry the run
 * clock forward on its own clock. Both are this window's: a remote host's clock never enters.
 */
export interface Heard {
  readonly run: number;
  readonly wallMs: number;
}

/** The run fold and trace a live or recorded run has produced so far. */
export interface RunFold {
  readonly core: CoreState | null;
  readonly trace: TraceLog;
  /** The run clock when the live run's spans last changed here; null before they did. */
  readonly heard: Heard | null;
}

export const EMPTY_RUN_FOLD: RunFold = {core: null, trace: EMPTY_TRACE_LOG, heard: null};

/** The latest run-clock time the spans carry that `data` brings, or null when it brings none. */
function timeOf(data: RunData): number | null {
  const spans =
    data.kind === 'snapshot'
      ? (data.snapshot.spans ?? [])
      : traceSpans(foldTrace(EMPTY_TRACE, data.events));
  return latestTime(spans);
}

/** What the window heard after `data` arrived at `wallMs`: the run clock moves only forward. */
export function heardAfter(heard: Heard | null, data: RunData, wallMs: number): Heard | null {
  if (data.kind === 'rebootstrap' && data.base === 'reset') return null;
  const run = timeOf(data);
  if (run === null || (heard !== null && run <= heard.run)) return heard;
  return {run, wallMs};
}

/** What must hold of the trace slice: its events are span events, in increasing sequence. */
export function traceProblems(log: TraceLog): string[] {
  const problems: string[] = [];
  let last = log.base.sequence;
  for (const event of log.events) {
    const at = event.sequence ?? 0;
    if (at <= last) problems.push(`trace event ${at} is not after ${last}`);
    if (!SPAN_EVENTS.has(event.type)) problems.push(`trace holds a ${event.type} event`);
    last = at;
  }
  return problems;
}
