/**
 * The scrubber: where it can stand in the run's trace log (`TraceLog`: the snapshot's trace and
 * the span events after it), and the view each position stands for. The tabs read the trace at
 * the view through `traceAt`, the same fold for the past and the present, so the scrubber can go
 * back as far as the snapshot the page started from.
 *
 * The scrubber stands at the snapshot or at a span event (where the trace changes); its newest
 * position follows the run while it is live, and is the run's end once it has ended.
 */
import type {RunEvent} from '@vibesys/contracts';
import type {TraceLog} from './live.js';
import type {RunView} from './run-page.js';

const SPAN_EVENTS: ReadonlySet<string> = new Set(['span_started', 'span_updated', 'span_ended']);

function sequenceOf(event: RunEvent): number {
  return event.sequence ?? 0;
}

/** The sequences the scrubber can stand at, oldest first: the snapshot's, then each span event's. */
export function scrubPositions(log: TraceLog): readonly number[] {
  const positions = [log.base.sequence];
  for (const event of log.events) {
    const at = sequenceOf(event);
    if (SPAN_EVENTS.has(event.type) && at > (positions.at(-1) ?? 0)) positions.push(at);
  }
  return positions;
}

/** What the scrubber shows: its playhead among the positions, its words, and its ticks. */
export interface ScrubberView {
  /** The index of the newest position. */
  readonly last: number;
  readonly value: number;
  /** Whether the view follows the newest position. */
  readonly latest: boolean;
  /** What the state reads at the newest position, and the action that returns there. */
  readonly latestLabel: string;
  readonly backLabel: string;
  /** The playhead in words: the time of the event it stands at, and the planning call by then. */
  readonly position: string;
  /** A tick where each planning call started. */
  readonly ticks: readonly {readonly at: number; readonly label: string}[];
}

/** The index of the last position at or before `at`. */
function indexOf(positions: readonly number[], at: number): number {
  let index = 0;
  for (let i = 0; i < positions.length; i += 1) {
    if ((positions[i] ?? 0) <= at) index = i;
  }
  return index;
}

function spanOf(event: RunEvent): {readonly kind?: string} | null {
  const data = event.data;
  if (!SPAN_EVENTS.has(event.type) || data === null || data === undefined || !('span' in data)) {
    return null;
  }
  return data.span.label ?? null;
}

/** The planning calls' starts: `[sequence, ordinal]`, oldest first. */
function planningCalls(log: TraceLog): readonly (readonly [number, number])[] {
  const calls: [number, number][] = [];
  for (const event of log.events) {
    if (event.type === 'span_started' && spanOf(event)?.kind === 'planning_call') {
      calls.push([sequenceOf(event), calls.length + 1]);
    }
  }
  return calls;
}

function clock(event: RunEvent | undefined): string | null {
  const stamp = event?.timestamp;
  return typeof stamp === 'string' && stamp.length >= 19 ? stamp.slice(11, 19) : null;
}

function positionWords(
  log: TraceLog,
  at: number,
  calls: readonly (readonly [number, number])[],
): string {
  const event = log.events.filter(each => sequenceOf(each) <= at).at(-1);
  const time = at === log.base.sequence ? null : clock(event);
  const call = calls.filter(([start]) => start <= at).at(-1)?.[1];
  const when = time ?? (log.base.sequence === 0 ? 'Start' : 'When the page connected');
  return call === undefined ? when : `${when} · planning call ${call}`;
}

/**
 * The scrubber at `view`, or null when the page has no event to scrub through yet. On an ended
 * run the newest position reads as the end rather than live.
 */
export function scrubberView(log: TraceLog, view: RunView, ended: boolean): ScrubberView | null {
  const positions = scrubPositions(log);
  const last = positions.length - 1;
  if (last === 0) return null;
  const value = view.at === 'live' ? last : indexOf(positions, view.at);
  const calls = planningCalls(log);
  return {
    last,
    value,
    latest: view.at === 'live',
    latestLabel: ended ? 'End' : 'Live',
    backLabel: ended ? 'Jump to end' : 'Back to live',
    position: positionWords(log, positions[value] ?? 0, calls),
    ticks: calls.map(([start, ordinal]) => ({
      at: indexOf(positions, start),
      label: `Call ${ordinal}`,
    })),
  };
}

/** The view the scrubber's position `index` stands for: the newest follows the run. */
export function viewAtPosition(log: TraceLog, index: number): RunView {
  const positions = scrubPositions(log);
  if (index >= positions.length - 1) return {at: 'live'};
  return {at: positions[Math.max(0, index)] ?? log.base.sequence};
}
