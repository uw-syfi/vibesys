/**
 * A host's runs as the views list them, from what the host's `runs` subscription says: the pure
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
  type ListingStep,
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
import {
  type ConnectStep,
  FAILURE_WORDS,
  type HostRunFacts,
  type RunFacts,
  type SubscriptionState,
} from '@vibesys/subscription';

const STAGES: Readonly<Record<StartupStage, NonNullable<RunPhase['stage']> | null>> = {
  [StartupStage.UNSPECIFIED]: null,
  [StartupStage.PROBING_WORKSPACE]: 'probing_workspace',
  [StartupStage.BUILDING_IMAGE]: 'building_image',
  [StartupStage.BUILDING_TOOLS]: 'building_tools',
  [StartupStage.STARTING_CONTAINER]: 'starting_container',
};

/** Why a run whose writer died without an ending reads as failed. */
const INTERRUPTED_REASON = 'The run stopped without recording an ending.';

/** The step of the way to a host's runs that a subscription's step is. */
const LISTING_STEP: Readonly<Record<ConnectStep, ListingStep>> = {
  gateway: 'gateway',
  handshake: 'handshake',
  stream: 'runs',
};

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

function listed(runId: string, run: RunFacts): Omit<HostRun, 'projectRoot'> {
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
export function watchedListing(host: HostKey, runs: HostRunFacts): HostListing {
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

/** The step a subscription has reached, as a listing names it; a value in hand means the last one. */
export function listingStep(previous: ListingStep, state: SubscriptionState<unknown>): ListingStep {
  switch (state.kind) {
    case 'connecting':
      return LISTING_STEP[state.step];
    case 'live':
      return 'runs';
    default:
      return previous;
  }
}

/** What the views say of a host while its subscription is not delivering. */
function waiting(
  state: Exclude<SubscriptionState<unknown>, {readonly kind: 'live'}>,
): ConnectionStatus {
  switch (state.kind) {
    case 'connecting':
      return state.attempt === 0
        ? {kind: 'connecting'}
        : {kind: 'reconnecting', attempt: state.attempt};
    case 'stale':
      return {kind: 'reconnecting', attempt: state.attempt};
    case 'failed':
      return {kind: 'offline', detail: FAILURE_WORDS[state.reason]};
  }
}

/**
 * The host's listing once its subscription is live, else why there is none yet. `step` is the
 * furthest the subscription has come (`listingStep`): where a failure is said to have stopped.
 */
export function hostListingFromRuns(
  host: HostKey,
  state: SubscriptionState<HostRunFacts>,
  step: ListingStep,
): HostListing {
  return state.kind === 'live'
    ? watchedListing(host, state.value)
    : {kind: 'unavailable', host, step, connection: waiting(state)};
}
