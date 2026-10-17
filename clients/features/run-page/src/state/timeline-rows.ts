/**
 * What the Timeline and the idea body draw as bars: the run's ideas as Gantt rows (running first,
 * then the rest newest first, as the Ideas list orders them) over a range of the run clock, the
 * rejected folded into one density lane when there are many; an idea's attempts as lines; and an
 * attempt's steps as segments (agent turn solid, gate hatched, judge grey).
 *
 * Pure over the candidates the run folded and the run clock; the views add presses and layout.
 */
import type {
  Candidate,
  CandidateAttempt,
  CandidateOutcome,
  CandidateStep,
} from '@vibesys/run-state/candidates';
import {figureOf} from './ideas-view.js';
import type {Direction, RunModel} from './run-model.js';
import {duration, formatValue, ratio, words} from './span-reading.js';

/** How much of the run the Timeline shows. */
export type TimelineRange = 'hour' | 'day' | 'all';
export const TIMELINE_RANGES: readonly {readonly value: TimelineRange; readonly label: string}[] = [
  {value: 'hour', label: 'Last hour'},
  {value: 'day', label: 'Last day'},
  {value: 'all', label: 'All'},
];
const RANGE_SECONDS: Readonly<Record<Exclude<TimelineRange, 'all'>, number>> = {
  hour: 3600,
  day: 86_400,
};

/** More rejected ideas than this in range fold into one lane (unless the reader expands them). */
export const REJECTED_DENSE = 24;
/** The folded lane has one mark per bin of this many divisions of its span. */
const LANE_BINS = 48;

export interface RunClockView {
  readonly start: number;
  readonly now: number | null;
}

type SegmentKind = 'solid' | 'hatched' | 'grey';

/** One step as a piece of a bar. */
export interface StepSegment {
  readonly id: string;
  readonly start: number;
  readonly end: number;
  readonly kind: SegmentKind;
  /** `role · headline · took`. */
  readonly label: string;
}

type BarOutcome = 'ok' | 'err' | 'live' | 'muted';

/** One idea's row; a `lane` row stands for folded rejected ideas and has no idea to open. */
export interface TimelineRow {
  readonly id: string;
  readonly label: string;
  readonly start: number;
  readonly end: number;
  readonly outcome: BarOutcome;
  readonly figure: string;
  readonly segments: readonly StepSegment[];
  readonly lane: boolean;
}

interface TimelineTick {
  readonly at: number;
  readonly label: string;
}

export interface TimelineRows {
  /** The whole axis (run start to the latest time reached) and the part in view. */
  readonly from: number;
  readonly to: number;
  readonly view: {readonly from: number; readonly to: number};
  readonly ticks: readonly TimelineTick[];
  readonly rows: readonly TimelineRow[];
  /** Rejected ideas in range that are folded into the lane (0 when none are). */
  readonly folded: number;
  /** Rejected ideas in range, folded or not. */
  readonly rejectedInRange: number;
  readonly total: number;
}

const OUTCOME: Readonly<Record<CandidateOutcome, BarOutcome>> = {
  running: 'live',
  kept: 'ok',
  rejected: 'err',
};

/** The end of something that began at `start`: its end, else the clock's now, else its start. */
export function endOf(start: number, endedAt: number | null, now: number | null): number {
  return Math.max(start, endedAt ?? now ?? start);
}

function stepKind(step: CandidateStep): SegmentKind {
  if (step.kind === 'gate') return 'hatched';
  return step.role === 'judge' ? 'grey' : 'solid';
}

/** The steps as bar segments; a running step reaches `now`. */
export function stepSegments(
  steps: readonly CandidateStep[],
  now: number | null,
): readonly StepSegment[] {
  return steps.map(step => {
    const end = endOf(step.startedAt, step.endedAt, now);
    const took = end > step.startedAt ? [duration(end - step.startedAt)] : [];
    return {
      id: step.span,
      start: step.startedAt,
      end,
      kind: stepKind(step),
      label: [step.role ?? words(step.kind), step.title, ...took].join(' · '),
    };
  });
}

function rowOf(item: Candidate, now: number | null): TimelineRow {
  const steps = item.attempts.flatMap(attempt => attempt.steps);
  return {
    id: item.id,
    label: item.title,
    start: item.startedAt,
    end: endOf(item.startedAt, item.endedAt, now),
    outcome: OUTCOME[item.outcome],
    figure: figureOf(item).figure,
    segments: stepSegments(steps, now),
    lane: false,
  };
}

function laneOf(rejected: readonly TimelineRow[]): TimelineRow {
  const start = Math.min(...rejected.map(row => row.start));
  const end = Math.max(...rejected.map(row => row.end));
  const width = (end - start) / LANE_BINS;
  const marked = new Set<number>();
  for (const row of rejected) {
    if (width <= 0) {
      marked.add(0);
      continue;
    }
    const first = Math.min(LANE_BINS - 1, Math.floor((row.start - start) / width));
    const last = Math.min(LANE_BINS - 1, Math.floor((row.end - start) / width));
    for (let bin = first; bin <= last; bin += 1) marked.add(bin);
  }
  const segments: StepSegment[] = [...marked]
    .sort((a, b) => a - b)
    .map(bin => ({
      id: `bin-${bin}`,
      start: start + bin * width,
      end: start + (bin + 1) * width,
      kind: 'grey',
      label: 'rejected ideas',
    }));
  return {
    id: 'rejected-lane',
    label: `+ ${rejected.length} rejected`,
    start,
    end,
    outcome: 'muted',
    figure: '',
    segments,
    lane: true,
  };
}

const TICK_STEPS = [60, 300, 600, 1800, 3600, 4 * 3600, 86_400, 7 * 86_400];
const MAX_TICKS = 8;

function tickLabel(offset: number): string {
  return offset >= 86_400 ? `d${Math.floor(offset / 86_400) + 1}` : duration(offset);
}

/** Ticks at a round step from the run's start, no more than `MAX_TICKS` over the view. */
function timelineTicks(
  start: number,
  view: {readonly from: number; readonly to: number},
): TimelineTick[] {
  const span = view.to - view.from;
  const step = TICK_STEPS.find(each => span / each <= MAX_TICKS) ?? TICK_STEPS.at(-1) ?? 60;
  const ticks: TimelineTick[] = [];
  const first = Math.ceil((view.from - start) / step) * step;
  for (let offset = Math.max(0, first); start + offset <= view.to; offset += step) {
    ticks.push({at: start + offset, label: tickLabel(offset)});
  }
  return ticks;
}

/** Running first, then the rest, each newest first (the candidates come in the order they began). */
function ordered(candidates: readonly Candidate[]): Candidate[] {
  const running: Candidate[] = [];
  const rest: Candidate[] = [];
  for (let at = candidates.length - 1; at >= 0; at -= 1) {
    const item = candidates[at] as Candidate;
    (item.outcome === 'running' ? running : rest).push(item);
  }
  return [...running, ...rest];
}

/**
 * The Timeline's rows over `range`: ideas that overlap the range, in the Ideas list's order. When
 * more than `REJECTED_DENSE` rejected ideas are in range and `expanded` is false, they fold into
 * one lane at the top.
 */
export function timelineRows(
  model: RunModel,
  range: TimelineRange,
  clock: RunClockView,
  expanded: boolean,
): TimelineRows {
  const all = model.candidates.candidates;
  const reached = Math.max(
    clock.now ?? clock.start,
    ...all.map(item => endOf(item.startedAt, item.endedAt, clock.now)),
  );
  const from = clock.start;
  const to = Math.max(reached, from + 1);
  const view = range === 'all' ? {from, to} : {from: Math.max(from, to - RANGE_SECONDS[range]), to};
  const inRange = ordered(all)
    .map(item => rowOf(item, clock.now))
    .filter(row => row.end >= view.from && row.start <= view.to);
  const rejected = inRange.filter(row => row.outcome === 'err');
  const fold = !expanded && rejected.length > REJECTED_DENSE;
  const rows = fold ? [laneOf(rejected), ...inRange.filter(row => row.outcome !== 'err')] : inRange;
  return {
    from,
    to,
    view,
    ticks: timelineTicks(clock.start, view),
    rows,
    folded: fold ? rejected.length : 0,
    rejectedInRange: rejected.length,
    total: all.length,
  };
}

/** One line of an idea's attempts. */
export interface AttemptLine {
  readonly span: string;
  /** `attempt 2`. */
  readonly title: string;
  /** `build failed`, `4.10×`, the verdict, or `running`. */
  readonly result: string;
  /** The measured value, `9,553,000 ops/s`; null when none. */
  readonly value: string | null;
  readonly tone: 'live' | 'ok' | 'err';
  /** Empty when unknown (running with no clock). */
  readonly took: string;
}

function resultOf(attempt: CandidateAttempt): string {
  if (attempt.outcome === 'running') return 'running';
  const measured = attempt.measurement;
  const failed = measured?.failed ?? null;
  if (failed !== null) return words(failed.check);
  if (measured?.vsBaseline !== null && measured?.vsBaseline !== undefined) {
    return ratio(measured.vsBaseline);
  }
  if (measured?.verdict !== null && measured?.verdict !== undefined) return words(measured.verdict);
  return attempt.reason === null ? 'rejected' : words(attempt.reason);
}

const ATTEMPT_TONE: Readonly<Record<CandidateOutcome, AttemptLine['tone']>> = {
  running: 'live',
  kept: 'ok',
  rejected: 'err',
};

/** An idea's attempts as lines, in the order they began. */
export function attemptLines(
  idea: Candidate,
  now: number | null,
  unit: string,
): readonly AttemptLine[] {
  return idea.attempts.map((attempt, at) => {
    const value = attempt.measurement?.metricValue ?? null;
    const known = attempt.endedAt !== null || now !== null;
    return {
      span: attempt.span,
      title: `attempt ${at + 1}`,
      result: resultOf(attempt),
      value: value === null ? null : `${formatValue(value)}${unit === '' ? '' : ` ${unit}`}`,
      tone: ATTEMPT_TONE[attempt.outcome],
      took: known
        ? duration(endOf(attempt.startedAt, attempt.endedAt, now) - attempt.startedAt)
        : '',
    };
  });
}

/** An attempt's steps as one bar over the attempt's own time. */
export interface AttemptStrip {
  readonly from: number;
  readonly to: number;
  readonly playhead: number | null;
  readonly row: TimelineRow;
}

export function attemptStrip(
  idea: Candidate,
  attempt: CandidateAttempt,
  now: number | null,
): AttemptStrip {
  const end = endOf(attempt.startedAt, attempt.endedAt, now);
  const vs = attempt.measurement?.vsBaseline ?? null;
  return {
    from: attempt.startedAt,
    to: Math.max(end, attempt.startedAt + 1),
    playhead: attempt.outcome === 'running' ? now : null,
    row: {
      id: attempt.span,
      label: `${idea.title} steps over time`,
      start: attempt.startedAt,
      end,
      outcome: OUTCOME[attempt.outcome],
      figure: attempt.outcome === 'running' || vs === null ? '' : ratio(vs),
      segments: stepSegments(attempt.steps, now),
      lane: false,
    },
  };
}

/** The idea's value against its parent's: 1.40× is better (the direction decides which way). */
export function versusParent(
  idea: Candidate,
  parent: Candidate | null,
  direction: Direction,
): string | null {
  const value = idea.measurement?.metricValue ?? null;
  const base = parent?.measurement?.metricValue ?? null;
  if (value === null || base === null || value === 0 || base === 0) return null;
  return ratio(direction === 'min' ? base / value : value / base);
}
