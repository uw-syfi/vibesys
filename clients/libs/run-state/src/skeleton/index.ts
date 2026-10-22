/** Store-to-view values for existing run readers. No legacy event replay or UI dependencies. */
import type {RunOverview} from '@vibesys/contracts';
import type {LogStream, RunStatus} from '@vibesys/contracts/host-helper';
import {
  Direction,
  DrainGoal,
  type Lifecycle,
  Outcome,
  type RunHead,
  StreamKind,
  StreamState,
} from '@vibesys/proto';
import type {CoreState} from '../state.js';
import {skeletonCore} from './core.js';
import {baselineFacts, capacityIntervals, comparisonFacts, inputFailures} from './facts.js';
import {measuredSpans} from './readings.js';
import {screenSpans} from './spans.js';
import {type StartupStep, startupSteps} from './startup.js';
import type {SkeletonContent, StoreHistory, StoreView} from './types.js';

export type {SkeletonContent, StoreHistory, StoreView} from './types.js';

/** Scalar screen values copied from the writer's committed primary measurement. */
export interface HeadlineReading {
  readonly series: {readonly name: string; readonly unit: string} | undefined;
  readonly inputValue: number | undefined;
  readonly bestValue: number | undefined;
}

export function readHeadline(head: RunHead | undefined, seq: bigint): HeadlineReading | undefined {
  if (head?.committedSeq !== seq || head.objective === undefined) return undefined;
  const objective = head.objective;
  return {
    series: objective.series
      ? {name: objective.series.name, unit: objective.series.unit}
      : undefined,
    inputValue: objective.inputValue,
    bestValue: objective.bestValue,
  };
}

export interface SkeletonReading {
  readonly spans: ReturnType<typeof screenSpans>;
  readonly overview: RunOverview | null;
  readonly direction: 'max' | 'min' | null;
  readonly latest: number | null;
  readonly core: CoreState;
  readonly phase: RunStatus | null;
  readonly comparisons: ReturnType<typeof comparisonFacts>;
  readonly baselines: ReturnType<typeof baselineFacts>;
  readonly pauses: ReturnType<typeof capacityIntervals>;
  readonly inputFailures: ReturnType<typeof inputFailures>;
  readonly startup: readonly StartupStep[];
  readonly streams: readonly LogStream[];
}
function screenSequence(seq: bigint): number {
  if (seq < 0n || seq > BigInt(Number.MAX_SAFE_INTEGER))
    throw new Error('sequence exceeds screen precision');
  return Number(seq);
}

function lifecycleStatus(lifecycle: Lifecycle | undefined): RunStatus | null {
  switch (lifecycle?.state.case) {
    case 'starting':
      return 'starting';
    case 'running':
      return lifecycle.state.value.hold ? 'paused' : 'running';
    case 'draining':
      return 'stopping';
    case 'parked':
      return 'paused';
    case 'ended':
      switch (lifecycle.state.value.outcome) {
        case Outcome.FINISHED:
          return 'completed';
        case Outcome.FAILED:
          return 'failed';
        case Outcome.STOPPED:
        case Outcome.INTERRUPTED:
          return 'stopped';
        default:
          return null;
      }
    default:
      return null;
  }
}

export function lifecycleState(lifecycle: Lifecycle | undefined) {
  const state = lifecycle?.state;
  const word =
    state?.case === 'parked'
      ? 'Paused'
      : state?.case === 'draining' && state.value.goal === DrainGoal.PARK
        ? 'Pausing'
        : state?.case === 'ended' && state.value.outcome === Outcome.INTERRUPTED
          ? 'Interrupted'
          : undefined;
  const reason =
    state?.case === 'ended'
      ? state.value.failure?.reason || state.value.summary || state.value.interruptionReason || null
      : null;
  return {status: lifecycleStatus(lifecycle), word, reason};
}

export function readSkeleton(
  skeleton: StoreHistory,
  view: StoreView,
  objectiveText = '',
  content?: SkeletonContent,
): SkeletonReading {
  screenSequence(view.seq);
  const epoch = view.epochs.at(-1)?.epoch;
  const run = epoch?.run;
  const objective = run?.objectives[0];
  const series = epoch?.series.find(item => item.name === objective?.series);
  const direction =
    objective?.direction === Direction.MAX
      ? 'max'
      : objective?.direction === Direction.MIN
        ? 'min'
        : null;
  const overview: RunOverview | null = run
    ? {
        objective: {
          text: objectiveText,
          metric: objective?.series ?? null,
          unit: series?.unit ?? null,
          direction,
          objectives: run.objectives.flatMap(item =>
            item.direction === Direction.MAX || item.direction === Direction.MIN
              ? [
                  {
                    name: item.series,
                    direction:
                      item.direction === Direction.MAX ? ('max' as const) : ('min' as const),
                  },
                ]
              : [],
          ),
          correctness_check: run.correctnessCheck,
        },
        benchmarks: {
          evaluator: run.evaluator
            ? {name: run.evaluator.name, version: run.evaluator.version}
            : null,
        },
      }
    : null;
  const spans = measuredSpans(screenSpans(Object.values(view.spans), skeleton), view);
  const times = [
    ...view.metrics.flatMap(history =>
      history.points.flatMap(item => (item.point ? [item.point.clock] : [])),
    ),
    ...spans.flatMap(span => [span.started_at, ...(span.ended_at == null ? [] : [span.ended_at])]),
  ];
  return {
    spans,
    overview,
    direction,
    comparisons: comparisonFacts(view),
    baselines: baselineFacts(view),
    pauses: capacityIntervals(view),
    inputFailures: inputFailures(skeleton, view),
    startup: startupSteps(skeleton, view.seq),
    latest: times.reduce<number | null>(
      (last, time) => (last === null ? time : Math.max(last, time)),
      null,
    ),
    core: skeletonCore(view, content),
    phase: lifecycleStatus(view.lifecycle),
    streams: skeleton.streams
      .filter(stream => stream.kind === StreamKind.LOG)
      .map(stream => ({
        id: stream.id,
        owner: {kind: 'run', span_id: stream.span ?? null},
        command: stream.title,
        retained_start: 0,
        size: screenSequence(stream.length ?? 0n),
        outcome:
          stream.state === StreamState.OPENED
            ? null
            : stream.state === StreamState.FAILED
              ? ('failed' as const)
              : ('ended' as const),
      })),
  };
}

/** All scrubber positions come from version histories, including those in the opening keyframe. */
export function skeletonPositions(skeleton: StoreHistory): readonly number[] {
  const seqs = new Set<bigint>([0n, skeleton.throughSeq ?? 0n]);
  for (const history of Object.values(skeleton.spans))
    for (const version of history.versions) seqs.add(version.seq);
  for (const version of skeleton.epochs) seqs.add(version.seq);
  for (const version of skeleton.lifecycles) seqs.add(version.seq);
  for (const history of skeleton.metrics) for (const point of history.points) seqs.add(point.seq);
  return [...seqs].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)).map(screenSequence);
}
