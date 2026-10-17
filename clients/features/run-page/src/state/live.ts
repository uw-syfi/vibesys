/**
 * The run's own data on the Run screen: the link the window holds to a live run (attach, then the
 * session's connection), what the session reports, the run fold (`CoreState`), and the trace's
 * span events (and the `run_started` event that declares the run's overview) in sequence order,
 * so every view folds the trace and the overview at the sequence it shows (`traceAt`, `overviewAt`):
 * the live end, or a past sequence the scrubber chose. History (an ended run's
 * recorded events, `runs.read_events`) folds into the same slice, so the tabs have one source.
 */
import type {RunEvent, RunOverview, RunSnapshot, Span} from '@vibesys/contracts';
import type {CoreState} from '@vibesys/run-state';
import {foldOverview, overviewFromSnapshot} from '@vibesys/run-state/overview';
import {
  EMPTY_TRACE,
  foldTrace,
  type RunTrace,
  traceFromSnapshot,
  traceSpans,
  type WorkMeter,
} from '@vibesys/run-state/trace';
import {AppendLog} from './append-log.js';
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
 * order, so it can be folded at any sequence from the base's on. What the live end needs is kept
 * as events arrive, each event read once: the fold (`head`), the scrubber's positions and
 * planning calls, and the latest run-clock time the spans carry. Appending a batch costs work in
 * proportion to the batch, however long the run.
 */
export interface TraceLog {
  readonly base: RunTrace;
  /** The overview at the base (a snapshot's), or null before one declared it. */
  readonly baseOverview: RunOverview | null;
  readonly events: AppendLog<RunEvent>;
  /** The trace with every event folded: the base's fold extended by each appended event. */
  readonly head: RunTrace;
  /** The overview with every event folded. */
  readonly headOverview: RunOverview | null;
  /** The sequences the scrubber can stand at, oldest first: the base's, then each span event's. */
  readonly positions: AppendLog<number>;
  /** The sequences where planning calls started, oldest first. */
  readonly calls: AppendLog<number>;
  /** The latest run-clock time the head's spans carry, or null when they carry none. */
  readonly latest: number | null;
}

const TRACE_SPAN_EVENTS: ReadonlySet<string> = new Set([
  'span_started',
  'span_updated',
  'span_ended',
]);

function spanOf(event: RunEvent): Span | null {
  const data = event.data;
  if (!TRACE_SPAN_EVENTS.has(event.type) || data === null || data === undefined) return null;
  return 'span' in data ? data.span : null;
}

function later(a: number | null, b: number | null): number | null {
  return a === null ? b : b === null || b <= a ? a : b;
}

/** `log` with `added` (span events after it, in order) read into its live end. */
function extended(log: TraceLog, added: readonly RunEvent[], meter?: WorkMeter): TraceLog {
  const positions: number[] = [];
  const calls: number[] = [];
  let latest = log.latest;
  for (const event of added) {
    const span = spanOf(event);
    if (span === null) continue;
    positions.push(event.sequence ?? 0);
    if (event.type === 'span_started' && span.label?.kind === 'planning_call') {
      calls.push(event.sequence ?? 0);
    }
    latest = later(latest, latestTime([span]));
  }
  return {
    ...log,
    events: log.events.append(added),
    // The head is the newest fold of its spans, so folding the new events costs one write each.
    head: foldTrace(log.head, added, meter),
    headOverview: foldOverview(log.headOverview, added),
    positions: log.positions.append(positions),
    calls: log.calls.append(calls),
    latest,
  };
}

/** The log of `events` after `base`, its live end read once. */
export function traceLog(
  base: RunTrace,
  baseOverview: RunOverview | null,
  events: readonly RunEvent[],
): TraceLog {
  const empty: TraceLog = {
    base,
    baseOverview,
    events: AppendLog.of<RunEvent>([]),
    head: base,
    headOverview: baseOverview,
    positions: AppendLog.of([base.sequence]),
    calls: AppendLog.of<number>([]),
    latest: latestTime(traceSpans(base)),
  };
  return events.length === 0 ? empty : extended(empty, events);
}

export const EMPTY_TRACE_LOG: TraceLog = traceLog(EMPTY_TRACE, null, []);

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
function appended(log: TraceLog, events: readonly RunEvent[], meter?: WorkMeter): TraceLog {
  let last = lastSequence(log);
  const added: RunEvent[] = [];
  for (const event of events) {
    const at = event.sequence ?? 0;
    if (at <= last) continue;
    last = at;
    if (SPAN_EVENTS.has(event.type)) added.push(event);
  }
  return added.length === 0 ? log : extended(log, added, meter);
}

/** The trace slice after `data`; `meter` counts the spans the live fold writes and copies. */
export function traceStep(log: TraceLog, data: RunData, meter?: WorkMeter): TraceLog {
  switch (data.kind) {
    case 'snapshot': {
      const base = traceFromSnapshot(data.snapshot);
      const after = log.events.slice(
        log.events.countWhile(event => (event.sequence ?? 0) <= base.sequence),
      );
      return traceLog(base, overviewFromSnapshot(data.snapshot), after);
    }
    case 'events':
      return appended(log, data.events, meter);
    case 'rebootstrap':
      return appended(
        data.base === 'preserve' ? traceLog(log.base, log.baseOverview, []) : EMPTY_TRACE_LOG,
        data.events,
        meter,
      );
  }
}

/** The events up to sequence `at`. */
function eventsAt(log: TraceLog, at: number): readonly RunEvent[] {
  return log.events.slice(
    0,
    log.events.countWhile(event => (event.sequence ?? 0) <= at),
  );
}

/**
 * The trace at sequence `at`, or at the live end when `at` is null. The live end is the log's
 * `head`, folded as events arrived, so following a run costs nothing per read; a past sequence
 * folds the base and the events up to it.
 */
export function traceAt(log: TraceLog, at: number | null): RunTrace {
  if (at === null || at >= lastSequence(log)) return log.head;
  return foldTrace(log.base, eventsAt(log, at));
}

/** The run's overview at sequence `at`, or at the live end when `at` is null. */
export function overviewAt(log: TraceLog, at: number | null): RunOverview | null {
  if (at === null || at >= lastSequence(log)) return log.headOverview;
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
  if (log.head.sequence !== Math.max(log.base.sequence, last)) {
    problems.push(`the live trace is at ${log.head.sequence}, the log at ${last}`);
  }
  return problems;
}
