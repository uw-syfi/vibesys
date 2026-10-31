/**
 * A host's runs as the views list them, from what the host's WatchRuns stream says: the pure
 * projection of each run's head and liveness into the registry listing the sidebar, notices and
 * run pages read. Runs without a validated project address remain listed in the typed unknownProject state.
 */
import {timestampMs} from '@bufbuild/protobuf/wkt';
import type {RunPhase} from '@vibesys/contracts/host-helper';
import {
  type ConnectionStatus,
  type HostKey,
  type HostListing,
  type HostRun,
  instancePhaseText,
} from '@vibesys/hosts';
import {
  DrainGoal,
  type Lifecycle,
  Outcome,
  type Progress,
  RunLiveness,
  StageState,
  type Starting,
  StartupStage,
} from '@vibesys/proto';
import type {RunsState, WatchedRun} from './shell/run-watch.js';

const STAGES: Readonly<Record<StartupStage, NonNullable<RunPhase['stage']> | null>> = {
  [StartupStage.UNSPECIFIED]: null,
  [StartupStage.PROBING_WORKSPACE]: 'probing_workspace',
  [StartupStage.BUILDING_IMAGE]: 'building_image',
  [StartupStage.BUILDING_TOOLS]: 'building_tools',
  [StartupStage.STARTING_CONTAINER]: 'starting_container',
};

/** Why a run whose writer died without an ending reads as failed. */
const INTERRUPTED_REASON = 'The run stopped without recording an ending.';

/** The failures after which the views call a host offline rather than reconnecting. */
const RECONNECT_OFFLINE_AFTER = 5;

interface LivePhase {
  readonly phase: HostRun['phase'];
  readonly paused: HostRun['paused'];
  /** The phase with the startup detail the words of a starting run need. */
  readonly detail: RunPhase;
}

function plain(status: HostRun['phase'], paused: HostRun['paused'] = null): LivePhase {
  return {phase: status, paused, detail: {status}};
}

/** A starting run's phase with its stage, and its progress while the stage is working. */
function startingPhase(starting: Starting): LivePhase {
  const named = STAGES[starting.stage];
  const {progress} = starting;
  const working = progress !== undefined && starting.state === StageState.RUNNING;
  return {
    phase: 'starting',
    paused: null,
    detail: {
      status: 'starting',
      ...(named === null ? {} : {stage: named}),
      ...(working ? {progress: progressOf(progress)} : {}),
    },
  };
}

function progressOf(progress: Progress): NonNullable<RunPhase['progress']> {
  return {
    done: Number(progress.done),
    ...(progress.total === undefined ? {} : {total: Number(progress.total)}),
    ...(progress.label === '' ? {} : {label: progress.label}),
  };
}

/** The registry phase of a run with a live writer, from the lifecycle that writer last published. */
function livePhase(lifecycle: Lifecycle | undefined): LivePhase {
  const state = lifecycle?.state;
  switch (state?.case) {
    case 'running': {
      const reason = state.value.hold?.reason.case;
      if (reason === 'operator') return plain('paused', 'user');
      return reason === 'capacitySpan' ? plain('paused', 'quota') : plain('running');
    }
    case 'draining':
      return plain(state.value.goal === DrainGoal.STOP ? 'stopping' : 'pausing');
    case 'starting':
      return startingPhase(state.value);
    default:
      return plain('starting');
  }
}

/** The phase and reason of a run with no live writer: how its lifecycle ended, or that it did not. */
function endedPhase(lifecycle: Lifecycle | undefined): {
  readonly phase: HostRun['phase'];
  readonly reason: string | null;
} {
  const state = lifecycle?.state;
  if (state?.case === 'ended') {
    switch (state.value.outcome) {
      case Outcome.FINISHED:
        return {phase: 'completed', reason: null};
      case Outcome.STOPPED:
        return {phase: 'stopped', reason: null};
      case Outcome.FAILED:
        return {phase: 'failed', reason: state.value.failure?.reason ?? state.value.summary};
      default:
        return {phase: 'failed', reason: state.value.interruptionReason ?? INTERRUPTED_REASON};
    }
  }
  if (state?.case === 'parked') return {phase: 'stopped', reason: null};
  return {phase: 'failed', reason: INTERRUPTED_REASON};
}

function listed(runId: string, run: WatchedRun): Omit<HostRun, 'projectRoot'> {
  const startedAt = run.head?.process?.startedAt;
  const common = {
    id: runId,
    runId,
    end: null,
    startedAt: startedAt === undefined ? 0 : timestampMs(startedAt) / 1000,
  };
  if (run.liveness === RunLiveness.ALIVE) {
    const {phase, paused, detail} = livePhase(run.head?.lifecycle);
    return {...common, status: instancePhaseText(detail), phase, paused, reason: null};
  }
  const {phase, reason} = endedPhase(run.head?.lifecycle);
  return {...common, status: phase, phase, paused: null, reason};
}

/** `runs` as `host`'s registry listing: live writers, ended or dead ones, and the unverifiable. */
export function watchedListing(host: HostKey, runs: ReadonlyMap<string, WatchedRun>): HostListing {
  const live: HostRun[] = [];
  const ended: HostRun[] = [];
  const unverified: string[] = [];
  const unknownProject: (Omit<HostRun, 'projectRoot'> & {alive?: boolean})[] = [];
  for (const [runId, run] of [...runs].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    if (run.projectRoot === undefined) {
      unknownProject.push({
        ...listed(runId, run),
        ...(run.liveness === RunLiveness.UNSPECIFIED
          ? {}
          : {alive: run.liveness === RunLiveness.ALIVE}),
      });
    } else if (run.liveness === RunLiveness.UNSPECIFIED) unverified.push(runId);
    else
      (run.liveness === RunLiveness.ALIVE ? live : ended).push({
        ...listed(runId, run),
        projectRoot: run.projectRoot,
      });
  }
  return {
    kind: 'listed',
    host,
    live,
    ended,
    unreadable: [],
    unverified,
    ...(unknownProject.length === 0 ? {} : {unknownProject}),
  };
}

/** What the views say of a host while its run stream is not delivering. */
function waiting(phase: Exclude<RunsState['phase'], {readonly kind: 'live'}>): ConnectionStatus {
  if (phase.kind === 'connecting') return {kind: 'connecting'};
  return phase.attempt >= RECONNECT_OFFLINE_AFTER
    ? {kind: 'offline', detail: phase.detail}
    : {kind: 'reconnecting', attempt: phase.attempt};
}

/** The host's listing once its stream has delivered every run, else why there is none yet. */
export function hostListingFromRuns(host: HostKey, state: RunsState): HostListing {
  const {phase} = state;
  return phase.kind === 'live'
    ? watchedListing(host, state.runs)
    : {kind: 'unavailable', host, step: phase.step, connection: waiting(phase)};
}
