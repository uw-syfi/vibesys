/**
 * Closing the run's open spans when the run is over. A run that ends badly (a crash, a stop, a
 * forced kill) leaves the spans it was working in as `running` with no end: nothing wrote their
 * `span_ended`. Read as they stand, they keep an idea "Running", give a step no duration and hold
 * the run clock where the last span event left it. `closeSpans` is the one place that settles
 * them, from facts the run itself recorded and without reading a clock: every span still open
 * ends, with the status the run's end gives it, and on the run's own time.
 *
 * A turn that finished before the run ended (it recorded its spend, with how long it took) ended
 * `done`, `took` after it started, whatever the rest of the run did. Any other open span ends
 * when the run's activity did: the latest time any span records, or the later time the run was
 * last heard (`heardAt`). Only differences between the run's own times enter, so a remote host's
 * clock that runs ahead or behind does no harm.
 */
import type {Span} from '@vibesys/contracts';

/** The spans of a run and the last sequence folded into them: what `RunTrace` is. */
interface ClosableTrace {
  readonly spans: ReadonlyMap<string, Span>;
  readonly sequence: number;
}

/** How the run's end settles a span that was still open: it finished, failed or was stopped. */
type EndedAs = 'done' | 'failed' | 'stopped';

/** One finished turn's recorded spend, as far as its duration goes (a `UsageRecord` is one). */
interface TurnTime {
  readonly span_id: string | null;
  readonly duration_ms: number | null;
}

/** What the run's end says about the spans left open. */
export interface RunEnding {
  /** The status the open spans end with. */
  readonly status: EndedAs;
  /** Why the run ended, when it said; carried on a span that ends failed or stopped. */
  readonly reason: string | null;
  /** The run-clock time the run was last heard at, when later than every span says; else null. */
  readonly heardAt: number | null;
  /** The turns that recorded their spend: each ended `done`, `duration_ms` after it started. */
  readonly turns: readonly TurnTime[];
}

const OPEN: ReadonlySet<Span['status']> = new Set(['pending', 'running', 'waiting']);

/** Whether `span` has not ended. */
export function isOpen(span: Span): boolean {
  return OPEN.has(span.status);
}

/** The latest run-clock time any span of `spans` records, or null with none. */
function latestTime(spans: Iterable<Span>): number | null {
  let latest: number | null = null;
  const see = (value: number | null | undefined): void => {
    if (value !== null && value !== undefined && (latest === null || value > latest))
      latest = value;
  };
  for (const span of spans) {
    see(span.started_at);
    see(span.ended_at);
    for (const wait of span.waiting_on ?? []) see(wait.since);
    for (const attempt of span.attempts ?? []) {
      see(attempt.started_at);
      see(attempt.ended_at);
    }
    see(span.stop?.requested_at);
  }
  return latest;
}

/** What each turn's span took, in seconds, by span id. */
function tookBy(turns: readonly TurnTime[]): ReadonlyMap<string, number> {
  const took = new Map<string, number>();
  for (const turn of turns) {
    if (turn.span_id === null || turn.duration_ms === null || !(turn.duration_ms >= 0)) continue;
    took.set(turn.span_id, (took.get(turn.span_id) ?? 0) + turn.duration_ms / 1000);
  }
  return took;
}

/** The words the run's end gives a span that ends with it, as a label's `reason`. */
function reasonOf(status: EndedAs): string {
  return `run_${status === 'stopped' ? 'stopped' : 'failed'}`;
}

function closed(span: Span, at: number, status: EndedAs, reason: string | null): Span {
  const endedAt = Math.max(span.started_at, at);
  const label = span.label;
  const named =
    status === 'done' || label === undefined || label === null || label.reason != null
      ? label
      : {...label, reason: reasonOf(status)};
  return {
    ...span,
    status,
    ended_at: endedAt,
    waiting_on: [],
    attempts: (span.attempts ?? []).map(attempt =>
      attempt.ended_at == null
        ? {...attempt, ended_at: Math.max(attempt.started_at, endedAt)}
        : attempt,
    ),
    failure:
      status === 'done' ? (span.failure ?? null) : {kind: 'interruption', message: reason ?? ''},
    ...(named === undefined ? {} : {label: named}),
  };
}

/**
 * `trace` with every open span ended as `ending` says. A trace with no open span is returned as it
 * is. Spans keep their order, start times and ids; each span that ends has `ended_at` at or after
 * its start, and closing twice changes nothing more.
 */
export function closeSpans(trace: ClosableTrace, ending: RunEnding): ClosableTrace {
  if (![...trace.spans.values()].some(isOpen)) return trace;
  const took = tookBy(ending.turns);
  const finished = new Map<string, number>();
  for (const span of trace.spans.values()) {
    const seconds = isOpen(span) ? took.get(span.id) : undefined;
    if (seconds !== undefined) finished.set(span.id, span.started_at + seconds);
  }
  const settled = Math.max(
    latestTime(trace.spans.values()) ?? Number.NEGATIVE_INFINITY,
    ...finished.values(),
    ending.heardAt ?? Number.NEGATIVE_INFINITY,
  );
  const spans = new Map<string, Span>();
  for (const [id, span] of trace.spans) {
    const turnEnd = finished.get(id);
    if (!isOpen(span)) spans.set(id, span);
    else if (turnEnd === undefined)
      spans.set(id, closed(span, settled, ending.status, ending.reason));
    else spans.set(id, closed(span, turnEnd, 'done', null));
  }
  return {spans, sequence: trace.sequence};
}
