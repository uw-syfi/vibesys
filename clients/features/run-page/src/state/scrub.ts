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
import type {AppendLog} from './append-log.js';
import type {TraceLog} from './live.js';
import type {RunView} from './run-page.js';

function sequenceOf(event: RunEvent): number {
  return event.sequence ?? 0;
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
  /** A tick where each planning call started, labelled with the call's number (`planning call 3` says what it counts). */
  readonly ticks: readonly {readonly at: number; readonly label: string}[];
}

/** The index of the last position at or before `at`, found by halving. */
function indexOf(positions: AppendLog<number>, at: number): number {
  return Math.max(0, positions.countWhile(position => position <= at) - 1);
}

/** The event's time of day (UTC), from its timestamp. */
function clock(event: RunEvent | undefined): string | null {
  const stamp = event?.timestamp;
  return typeof stamp !== 'string' || stamp.length < 19 ? null : stamp.slice(11, 19);
}

/** The run-clock time `seconds` (epoch seconds on the run's host) as a time of day (UTC). */
function timeOfDay(seconds: number | null): string | null {
  if (seconds === null) return null;
  const ms = seconds * 1000;
  return Number.isFinite(ms) ? new Date(ms).toISOString().slice(11, 19) : null;
}

function positionWords(log: TraceLog, at: number, wall: number | null): string {
  if (log.earlier !== null && at < log.handoff) return positionWords(log.earlier, at, wall);
  const event = log.events.lastWhile(each => sequenceOf(each) <= at);
  // At the live end the time is the host's wall time now; at the base it is the snapshot's (the
  // latest its spans carry); anywhere else it is the time of the event.
  const time =
    wall !== null
      ? timeOfDay(wall)
      : at === log.base.sequence
        ? timeOfDay(log.baseLatest)
        : clock(event);
  const call = log.calls.countWhile(start => start <= at);
  const when = time ?? (log.base.sequence === 0 ? 'Start' : 'When the page connected');
  return call === 0 ? when : `${when} · planning call ${call}`;
}

/**
 * The scrubber at `view`, or null when the page has neither an event nor a snapshot time yet. A
 * page opened on a snapshot with no event since still shows the row: one position, at the
 * snapshot's time. On an ended run the newest position reads as the end rather than live. It
 * reads the positions and planning calls the log keeps, so its work is in the planning calls, not
 * the run's events. A live run's newest position reads the host's wall time now (`wall`, epoch
 * seconds, `runWallNow`), not an event's time: a step that runs for minutes says nothing, and the
 * clock must still follow real time.
 */
export function scrubberView(
  log: TraceLog,
  view: RunView,
  ended: boolean,
  wall: number | null = null,
): ScrubberView | null {
  const {positions} = log;
  const last = positions.length - 1;
  // With only the base to stand at, the row still reads the clock when the snapshot has a time.
  if (last === 0 && log.baseLatest === null) return null;
  const value = view.at === 'live' ? last : indexOf(positions, view.at);
  return {
    last,
    value,
    latest: view.at === 'live',
    latestLabel: ended ? 'End' : 'Live',
    backLabel: ended ? 'Jump to end' : 'Back to live',
    position: positionWords(
      log,
      positions.at(value) ?? 0,
      view.at === 'live' && !ended ? wall : null,
    ),
    ticks: [...log.calls].map((start, index) => ({
      at: indexOf(positions, start),
      label: String(index + 1),
    })),
  };
}

/** The view the scrubber's position `index` stands for: the newest follows the run. */
export function viewAtPosition(log: TraceLog, index: number): RunView {
  const {positions} = log;
  if (index >= positions.length - 1) return {at: 'live'};
  return {at: positions.at(Math.max(0, index)) ?? log.base.sequence};
}
