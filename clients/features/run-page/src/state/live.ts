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
import {foldInputGates, type InputGates, joinedInputGates, NO_INPUT_GATES} from './input-gates.js';
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
  /** The latest run-clock time the base's spans carry: when the snapshot was taken, or null. */
  readonly baseLatest: number | null;
  /** The gates that failed before the first idea began (see `input-gates.ts`). */
  readonly inputGates: InputGates;
  /**
   * The log this one replaced when a snapshot or a replay moved its base, so the scrubber keeps
   * what it could reach: `positions` and `calls` cover both logs, and a sequence below `handoff`
   * is read from `earlier` (what the page itself had folded then), not from this base.
   */
  readonly earlier: TraceLog | null;
  readonly handoff: number;
}

function isWorkstream(span: Span): boolean {
  return span.label?.kind === 'workstream';
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
  let inputGates = log.inputGates;
  for (const event of added) {
    inputGates = foldInputGates(inputGates, event);
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
    inputGates,
  };
}

/** The log of `events` after `base`, its live end read once. */
export function traceLog(
  base: RunTrace,
  baseOverview: RunOverview | null,
  events: readonly RunEvent[],
): TraceLog {
  const baseLatest = latestTime(traceSpans(base));
  const empty: TraceLog = {
    base,
    baseOverview,
    events: AppendLog.of<RunEvent>([]),
    head: base,
    headOverview: baseOverview,
    positions: AppendLog.of([base.sequence]),
    calls: AppendLog.of<number>([]),
    latest: baseLatest,
    baseLatest,
    inputGates: {...NO_INPUT_GATES, settled: traceSpans(base).some(isWorkstream)},
    earlier: null,
    handoff: base.sequence,
  };
  return events.length === 0 ? empty : extended(empty, events);
}

/** The sequences of `positions` below `handoff`. */
function before(positions: AppendLog<number>, handoff: number): number[] {
  return positions.slice(
    0,
    positions.countWhile(position => position < handoff),
  );
}

/**
 * `next` (a log that replaced `previous`) with the scrubber's reach kept: the positions and planning
 * calls of `previous` below `handoff` stay, and those sequences are read from `previous`. A log
 * with nothing to scrub to has nothing to keep.
 */
function replacing(next: TraceLog, previous: TraceLog, handoff: number): TraceLog {
  if (previous.positions.length <= 1) return next;
  return {
    ...next,
    positions: AppendLog.of([
      ...before(previous.positions, handoff),
      ...next.positions.slice(next.positions.countWhile(position => position < handoff)),
    ]),
    calls: AppendLog.of([
      ...before(previous.calls, handoff),
      ...next.calls.slice(next.calls.countWhile(start => start < handoff)),
    ]),
    earlier: previous,
    handoff,
  };
}

export const EMPTY_TRACE_LOG: TraceLog = traceLog(EMPTY_TRACE, null, []);

/**
 * The events the slice keeps: the trace's span events, the overview's declaration and the gates
 * that finished. The host selects the same events when asked for the `trace` kind of a history
 * read (`RUN_EVENT_TYPES_BY_KINDS`, which `live.test.ts` holds this set to).
 */
export const SPAN_EVENTS: ReadonlySet<string> = new Set([
  'span_started',
  'span_updated',
  'span_ended',
  'run_started',
  'gate_finished',
]);

/** The events of `events` the trace slice keeps (`SPAN_EVENTS`), in their order. */
export function traceEventsOf(events: readonly RunEvent[]): RunEvent[] {
  return events.filter(event => SPAN_EVENTS.has(event.type));
}

/** Whether an event of this type is one the trace slice keeps: the rest need not be read to scrub. */
export function isTraceEventType(type: unknown): boolean {
  return typeof type === 'string' && SPAN_EVENTS.has(type);
}

/** What the session folded into its run, as the trace slice needs it. */
export type RunData =
  | {
      readonly kind: 'snapshot';
      readonly snapshot: RunSnapshot;
      /**
       * The `gate_finished` events before the snapshot's first idea, when the snapshot comes from
       * a summary: its spans hold no gate event, so the page folds these as a journal's are.
       */
      readonly gates?: readonly RunEvent[];
    }
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

/**
 * `log` with the scrubber's reach extended back through `events` older than its base. A base from a
 * summary stands at the run's newest event, and the session's replay can carry events before it:
 * the fold has them already, so they add nothing to the head, but they are positions the bar can
 * stand at. They are read into `earlier` (what the page heard then), as a replaced log's are, so a
 * view at one of them reads the trace as the page heard it.
 */
function reachedBack(log: TraceLog, events: readonly RunEvent[], meter?: WorkMeter): TraceLog {
  const known = log.earlier === null ? 0 : lastSequence(log.earlier);
  const older = events.filter(event => {
    const at = event.sequence ?? 0;
    return SPAN_EVENTS.has(event.type) && at < log.handoff && at > known;
  });
  if (older.length === 0) return log;
  const earlier = appended(log.earlier ?? EMPTY_TRACE_LOG, older, meter);
  return replacing(log, earlier, log.handoff);
}

/** `log` with the failures and passes of `gates` joined to its own, whichever came first. */
function withInputFailures(log: TraceLog, gates: InputGates): TraceLog {
  const joined = joinedInputGates(log.inputGates, gates.failures, gates.passes);
  return joined === log.inputGates ? log : {...log, inputGates: joined};
}

/**
 * `log` with the input gates `gates` hold: failures read by `foldInputGates` from before any idea
 * began, whatever the spans say (they say whether an idea began, not what the gates before it did).
 * A summary of the run, or its journal, is where they come from once the page joined a run late.
 */
export function withInputGates(log: TraceLog, gates: readonly RunEvent[]): TraceLog {
  return withInputFailures(log, gates.reduce(foldInputGates, NO_INPUT_GATES));
}

/** The trace slice after `data`; `meter` counts the spans the live fold writes and copies. */
export function traceStep(log: TraceLog, data: RunData, meter?: WorkMeter): TraceLog {
  switch (data.kind) {
    case 'snapshot': {
      const base = traceFromSnapshot(data.snapshot);
      const after = log.events.slice(
        log.events.countWhile(event => (event.sequence ?? 0) <= base.sequence),
      );
      // A snapshot says nothing of the gates before the first idea: those the page already holds
      // (and those a summary brings) stay, whichever came first.
      const kept: InputGates = {
        ...log.inputGates,
        failures: log.inputGates.failures.filter(failure => failure.sequence <= base.sequence),
      };
      const fresh = traceLog(base, overviewFromSnapshot(data.snapshot), after);
      return replacing(
        withInputFailures(withInputGates(fresh, data.gates ?? []), kept),
        log,
        base.sequence,
      );
    }
    case 'events':
      return appended(reachedBack(log, data.events, meter), data.events, meter);
    case 'rebootstrap': {
      if (data.base === 'reset') return appended(EMPTY_TRACE_LOG, data.events, meter);
      const replayed = withInputFailures(
        appended(traceLog(log.base, log.baseOverview, []), data.events, meter),
        log.inputGates,
      );
      // What the replay does not replace stays reachable: the page's own earlier fold.
      return replacing(replayed, log, replayed.positions.at(1) ?? log.base.sequence);
    }
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
  if (log.earlier !== null && at < log.handoff) return traceAt(log.earlier, at);
  return foldTrace(log.base, eventsAt(log, at));
}

/** The run's overview at sequence `at`, or at the live end when `at` is null. */
export function overviewAt(log: TraceLog, at: number | null): RunOverview | null {
  if (at === null || at >= lastSequence(log)) return log.headOverview;
  if (log.earlier !== null && at < log.handoff) return overviewAt(log.earlier, at);
  return foldOverview(log.baseOverview, eventsAt(log, at));
}

/**
 * How the run's own clock relates to its events' timestamps (`stamp`, the host's wall time): a
 * span that began or ended at run time `t` in an event stamped `s` says the two differ by `s - t`.
 * Both come from the run's host, so the difference is the host's alone, and it never involves this
 * machine's clock. The newest such event gives it.
 */
interface ClockOffset {
  /** Seconds: a stamp minus the run time it was stamped at. */
  readonly seconds: number;
  /** The stamp (epoch seconds) of the event that told it, so a replayed older event cannot undo it. */
  readonly at: number;
}

/**
 * Where the run clock stood when this window last heard it advance, and this window's clock
 * (epoch ms) then. The run's own clock is read from its spans, which change when a step does, so a
 * step that runs for minutes shows no time passing; the events' timestamps, mapped onto the run
 * clock by `offset`, carry it forward, and `runNow` carries it on this window's clock from there.
 * Only the run's own times and their differences enter, never their value against this window's
 * clock, so a host whose clock runs ahead or behind does no harm; and the newest time wins, so
 * events heard late or replayed (history, a reconnect's tail) move nothing.
 */
export interface Heard {
  readonly run: number;
  readonly offset: ClockOffset | null;
  readonly wallMs: number;
}
/** The run fold and trace a live or recorded run has produced so far. */
export interface RunFold {
  readonly core: CoreState | null;
  readonly trace: TraceLog;
  /** Where the live run's own clock stood when this window last heard it advance; null before. */
  readonly heard: Heard | null;
}

export const EMPTY_RUN_FOLD: RunFold = {core: null, trace: EMPTY_TRACE_LOG, heard: null};

/** An event's timestamp as epoch seconds, or null when it carries none. */
function stampSeconds(event: RunEvent): number | null {
  const ms = typeof event.timestamp === 'string' ? Date.parse(event.timestamp) : Number.NaN;
  return Number.isNaN(ms) ? null : ms / 1000;
}

/** What the window knows of the run clock while it folds events: its time, and its offset. */
interface HeardClock {
  readonly run: number | null;
  readonly offset: ClockOffset | null;
}

/**
 * The run time a span event stamped at `stamp` happened at, when its span says so: a span that
 * began or ended in it did so then, and so did any change that brings a time later than the run
 * clock held. A change with no such time (a label, a token count) says nothing of when it was.
 */
function stampedAt(event: RunEvent, span: Span, run: number | null): number | null {
  if (event.type === 'span_started') return span.started_at;
  if (event.type === 'span_ended' && span.ended_at != null) return span.ended_at;
  const time = latestTime([span]);
  return time !== null && (run === null || time > run) ? time : null;
}

/** The run clock after `event`: a later span time, else the event's stamp on the run clock. */
function heardEvent(heard: HeardClock, event: RunEvent): HeardClock {
  const span = spanOf(event);
  const stamp = stampSeconds(event);
  let {run, offset} = heard;
  const at = span === null || stamp === null ? null : stampedAt(event, span, run);
  if (stamp !== null && at !== null && (offset === null || stamp >= offset.at)) {
    offset = {seconds: stamp - at, at: stamp};
  }
  const time = span === null ? null : latestTime([span]);
  if (time !== null && (run === null || time > run)) run = time;
  if (stamp !== null && offset !== null) {
    const stampedRun = stamp - offset.seconds;
    if (run === null || stampedRun > run) run = stampedRun;
  }
  return {run, offset};
}

/** What the window heard from a snapshot: the latest time its spans carry, when that is later. */
function heardSnapshot(heard: Heard | null, snapshot: RunSnapshot, wallMs: number): Heard | null {
  const run = latestTime(snapshot.spans ?? []);
  if (run === null || (heard !== null && run <= heard.run)) return heard;
  return {run, offset: heard?.offset ?? null, wallMs};
}

/** What the window heard from `events`: the run clock after each, moving only forward. */
function heardEvents(heard: Heard | null, events: readonly RunEvent[], wallMs: number) {
  let next: HeardClock = heard ?? {run: null, offset: null};
  for (const event of events) next = heardEvent(next, event);
  if (next.run === null) return heard;
  if (heard !== null && next.run === heard.run) {
    return next.offset === heard.offset ? heard : {...heard, offset: next.offset};
  }
  return {run: next.run, offset: next.offset, wallMs};
}

/**
 * What the window heard after `data` arrived at `wallMs`: the run clock moves only forward, by the
 * span times the data carries and by its events' stamps on the run clock.
 */
export function heardAfter(heard: Heard | null, data: RunData, wallMs: number): Heard | null {
  if (data.kind === 'rebootstrap' && data.base === 'reset') return null;
  if (data.kind === 'snapshot') return heardSnapshot(heard, data.snapshot, wallMs);
  return heardEvents(heard, data.events, wallMs);
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
