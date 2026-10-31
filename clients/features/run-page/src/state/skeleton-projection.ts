/** Skeleton readings are projected into the existing Run screen props; seeking is local. */
import {type HostRun, recordedProjectRoot} from '@vibesys/hosts';
import {
  type LoadProgress,
  observeRun,
  type RunLivenessHint,
  type Skeleton,
  seekSkeleton,
} from '@vibesys/run-skeleton';
import {agentGraph} from '@vibesys/run-state/agents';
import {candidateTree} from '@vibesys/run-state/candidates';
import {type RecordRow, rowsOfRecords} from '@vibesys/run-state/records';
import {
  clockThrough,
  coreIdeaKey,
  coreIdeaMeasurements,
  lifecycleState,
  readHeadline,
  readSkeleton,
  type SkeletonContent,
  skeletonPositions,
} from '@vibesys/run-state/skeleton';
import {AppendLog} from './append-log.js';
import {EMPTY_TRACE_LOG} from './live.js';
import {bestSteps, readObjectives} from './objective-reading.js';
import {clockStart} from './run-level.js';
import type {RunModel} from './run-model.js';
import {type RunPageSeed, type RunPageState, runPageInitial} from './run-page.js';
import {marksAlong, placesAlong, runningTimeOf, runningWords} from './running-time.js';
import {settleIdeas} from './settled-ideas.js';
import {skeletonPerformance} from './skeleton-performance.js';
import {STARTUP_LABELS} from './startup-stages.js';

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
  options: {
    head?: RunLivenessHint['head'];
    content?: SkeletonContent;
    progress?: LoadProgress;
    loadFailure?: string | undefined;
  } = {},
): RunModel {
  const head =
    options.head?.performanceStated && options.head.committedSeq === seq
      ? options.head
      : skeleton.head;
  const headline = readHeadline(head, seq);
  const view = seekSkeleton(skeleton, seq);
  const reading = readSkeleton(skeleton, view, objectiveText, options.content);
  const tree = coreIdeaMeasurements(
    settleIdeas(candidateTree(reading.spans), reading.comparisons),
    head,
    seq,
  );
  const leader = tree.candidates.find(idea => coreIdeaKey(idea) === headline?.leader)?.id;
  const bestPath: string[] = [];
  const byId = new Map(tree.candidates.map(idea => [idea.id, idea]));
  const seen = new Set<string>();
  for (
    let idea = byId.get(leader ?? '');
    idea && !seen.has(idea.id);
    idea = byId.get(idea.parent ?? '')
  ) {
    seen.add(idea.id);
    bestPath.unshift(idea.id);
  }
  const candidates = {
    ...tree,
    best: leader
      ? {candidate: leader, adopted: tree.best?.candidate === leader && tree.best.adopted}
      : null,
    bestPath,
  };
  return {
    progress: options.progress,
    loadFailure: options.loadFailure,
    headline,
    series: skeletonPerformance(view, reading.spans, candidates.candidates, head),
    input: reading.input,
    at: Number(seq),
    trace: {spans: new Map(reading.spans.map(span => [span.id, span])), sequence: Number(seq)},
    overview: reading.overview,
    direction: reading.direction,
    candidates,
    agents: agentGraph(reading.spans),
    pauses: reading.pauses,
    baselines: reading.baselines,
    latest: reading.latest,
    sessions: reading.sessions,
    inputGates: reading.inputFailures,
    changed: new Set(),
  };
}
/**
 * The current epoch's latest failure: the keyframe's own error notices and the lifecycle failures
 * and notices of the record log, whichever is later. The keyframe keeps the notices a log window
 * no longer reaches.
 */
function latestFailure(
  skeleton: Skeleton,
  recordLog: readonly RecordRow[] | undefined,
): RecordRow | undefined {
  const epoch = observeRun(skeleton).epoch;
  // A failure after the prefix it claims to summarise is no fact of it: never the banner.
  const kept = rowsOfRecords(
    skeleton.failures.filter(record => record.seq <= (skeleton.throughSeq ?? 0n)),
  );
  return [...kept, ...(recordLog ?? [])]
    .filter(row => row.failure && row.epoch === epoch)
    .sort((a, b) => (BigInt(a.seq) < BigInt(b.seq) ? -1 : 1))
    .at(-1);
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
  // Usage is a run-wide ledger; seeking changes transcript visibility, never its totals or identity.
  const currentCore =
    ui.view.at === 'live'
      ? reading.core
      : readSkeleton(skeleton, seekSkeleton(skeleton, skeleton.throughSeq ?? 0n), '', content).core;
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
  const positions = skeletonPositions(skeleton);
  const trace = {
    ...EMPTY_TRACE_LOG,
    base: whole.trace,
    head: whole.trace,
    baseOverview: whole.overview,
    headOverview: whole.overview,
    positions: AppendLog.of(positions),
    calls: AppendLog.of(
      Object.values(skeleton.spans)
        .flatMap(history => {
          const start = history.versions[0];
          return start?.span?.label?.kind === 'planning_call' ? [Number(start.seq)] : [];
        })
        .sort((left, right) => left - right),
    ),
    latest: whole.latest,
    baseLatest: whole.latest,
  };
  return {
    ...ui,
    progress: whole.progress,
    recordDiagnostic: latestFailure(skeleton, ui.recordLog),
    lifecycleWord: lifecycle.word,
    ...timeBar(skeleton, whole, reading.latest, positions),
    startupStages: reading.startup.map(step => ({
      id: step.stage,
      label: STARTUP_LABELS[step.stage],
      state: step.state,
      streamId:
        reading.streams
          .filter(
            stream =>
              stream.owner['kind'] === 'startup_step' && stream.owner['stage'] === step.stage,
          )
          .at(-1)?.id ?? null,
    })),
    readOnly: true,
    record:
      run === null ? {kind: 'historical', status: null} : {kind: ended ? 'ended' : 'live', run},
    history: {kind: 'read'},
    session: null,
    run: {
      ...runPageInitial(seed).run,
      trace,
      core: {...currentCore, transcript: reading.core.transcript},
    },
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

/**
 * The time bar's word and track: the run's running time, the same clock as the charts' axes. The
 * word is the time of the view (`latest` on the run clock), the track spaces `positions` by it, and a
 * mark on it is each new best of the charts' lines.
 */
function timeBar(
  skeleton: Skeleton,
  whole: RunModel,
  latest: number | null,
  positions: readonly number[],
): Pick<RunPageState, 'positionWord' | 'positionPlaces' | 'positionMarks'> {
  const start = clockStart(whole);
  const time = runningTimeOf(whole.sessions ?? [], start);
  const seconds = clockThrough(skeleton, positions).map(at => time.at(at ?? start));
  const newBests = readObjectives(whole).flatMap(reading =>
    bestSteps(reading).map(step => time.at(step.at)),
  );
  return {
    positionWord:
      whole.loadFailure ?? positionWord(whole.progress, latest === null ? null : time.at(latest)),
    positionPlaces: placesAlong(seconds),
    positionMarks: marksAlong(seconds, newBests),
  };
}

function positionWord(progress: LoadProgress | undefined, running: number | null): string {
  if (progress && progress.phase !== 'complete') return 'Catching up';
  return running === null ? 'Start' : runningWords(running);
}
