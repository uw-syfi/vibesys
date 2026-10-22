/** Skeleton readings are projected into the existing Run screen props; seeking is local. */
import {type HostRun, recordedProjectRoot} from '@vibesys/hosts';
import type {RunHead} from '@vibesys/proto';
import {observeRun, type RunLivenessHint, type Skeleton, seekSkeleton} from '@vibesys/run-skeleton';
import {agentGraph} from '@vibesys/run-state/agents';
import {candidateTree} from '@vibesys/run-state/candidates';
import {
  lifecycleState,
  readSkeleton,
  type SkeletonContent,
  skeletonPositions,
} from '@vibesys/run-state/skeleton';
import {AppendLog} from './append-log.js';
import {EMPTY_TRACE_LOG} from './live.js';
import {withLiveBest} from './live-best.js';
import type {RunModel} from './run-model.js';
import {type RunPageSeed, type RunPageState, runPageInitial} from './run-page.js';
import {settleIdeas} from './settled-ideas.js';
import {duration} from './span-reading.js';

export {skeletonPositions} from '@vibesys/run-state/skeleton';

/** The absolute project root an epoch header recorded, latest one that has it. */
export function recordedRunRoot(skeleton: Skeleton | undefined) {
  if (skeleton === undefined) return undefined;
  for (let index = skeleton.epochs.length - 1; index >= 0; index -= 1) {
    const root = recordedProjectRoot(skeleton.epochs[index]?.epoch?.projectRoot);
    if (root !== undefined) return root;
  }
  return undefined;
}

export function projectSkeleton(
  skeleton: Skeleton,
  seq: bigint,
  objectiveText = '',
  options: {head?: RunHead | undefined; content?: SkeletonContent} = {},
): RunModel {
  const reading = readSkeleton(
    skeleton,
    seekSkeleton(skeleton, seq),
    objectiveText,
    options.content,
  );
  return {
    headline: options.head?.committedSeq === seq ? options.head.objective : undefined,
    at: Number(seq),
    trace: {spans: new Map(reading.spans.map(span => [span.id, span])), sequence: Number(seq)},
    overview: reading.overview,
    direction: reading.direction,
    candidates: withLiveBest(
      settleIdeas(candidateTree(reading.spans), reading.comparisons),
      reading.direction,
    ),
    agents: agentGraph(reading.spans),
    pauses: reading.pauses,
    baselines: reading.baselines,
    latest: reading.latest,
    inputGates: reading.inputFailures,
    changed: new Set(),
  };
}
export function skeletonScreenState(
  seed: RunPageSeed,
  skeleton: Skeleton,
  whole: RunModel,
  ui: RunPageState,
  hint?: RunLivenessHint,
  content?: SkeletonContent,
): RunPageState {
  const reading = readSkeleton(
    skeleton,
    seekSkeleton(
      skeleton,
      ui.view.at === 'live' ? (skeleton.throughSeq ?? 0n) : BigInt(ui.view.at),
    ),
    '',
    content,
  );
  const lifecycle = observedLifecycle(skeleton, hint);
  const status = lifecycle.status;
  const ended = status === 'completed' || status === 'stopped' || status === 'failed';
  const run: HostRun | null =
    status === null
      ? null
      : {
          id: seed.identity.runId,
          runId: seed.identity.runId,
          projectRoot: recordedRunRoot(skeleton) ?? '',
          phase: status,
          status,
          paused: status === 'paused' ? 'user' : null,
          end: null,
          startedAt: 0,
          reason: lifecycle.reason,
        };
  const trace = {
    ...EMPTY_TRACE_LOG,
    base: whole.trace,
    head: whole.trace,
    baseOverview: whole.overview,
    headOverview: whole.overview,
    positions: AppendLog.of(skeletonPositions(skeleton)),
    latest: whole.latest,
    baseLatest: whole.latest,
  };
  return {
    ...ui,
    recordDiagnostic: ui.recordLog
      ?.filter(row => row.failure && row.epoch === observeRun(skeleton).epoch)
      .at(-1),
    lifecycleWord: lifecycle.word,
    positionWord: reading.latest === null ? 'Start' : duration(reading.latest),
    startupStages: reading.startup.map(step => ({
      id: step.stage,
      label: (
        {
          probing_workspace: 'Workspace',
          building_image: 'Image',
          building_tools: 'Tools',
          starting_container: 'Container',
        } as const
      )[step.stage],
      state: step.state,
      streamId: null,
    })),
    readOnly: true,
    record:
      run === null ? {kind: 'historical', status: null} : {kind: ended ? 'ended' : 'live', run},
    history: {kind: 'read'},
    session: null,
    run: {...runPageInitial(seed).run, trace, core: reading.core},
    logs: {
      kind: 'listed',
      instanceId: seed.identity.runId,
      streams: ui.view.at === 'live' ? reading.streams : [],
    },
    view: ui.view,
  };
}

function observedLifecycle(skeleton: Skeleton, hint: RunLivenessHint | undefined) {
  const observation = observeRun(skeleton, hint);
  return observation.interrupted
    ? {status: 'failed' as const, word: 'Interrupted', reason: null}
    : lifecycleState(observation.lifecycle);
}
