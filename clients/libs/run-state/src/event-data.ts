/**
 * The typed event-data handlers: which part of the run fold each `RunEvent['data']` kind updates,
 * and the run status and lifecycle transitions.
 */
import type {RunEvent} from '@vibesys/contracts';
import {benchmarkRecordFromEvent} from './benchmark.js';
import {applyChatProposal} from './chat.js';
import {
  applyAgentExecutionActivityData,
  applyAgentExecutionFinishedData,
  applyAgentExecutionStartedData,
  applyAgentStatusData,
  applyTerminalExecutionStatus,
  reconcileStartedExecutionStatus,
  removeFinishedExecutionStatus,
  updateTodos,
} from './executions.js';
import {
  type CoreRunStatus,
  type CoreState,
  cloneCoreStateWith,
  type EndedRunStatus,
  endedRunStatus,
  hasRunEnded,
  type MutableCoreState,
  type QuotaPause,
  type QuotaState,
  type QuotaStop,
} from './state.js';
import {applySteerDelivery} from './steer-delivery.js';
import {ledgerWith} from './token-usage.js';
import {appendTranscript, eventToTranscriptEntry, RUN_TRANSCRIPT} from './transcript-fold.js';
import type {TranscriptFolder} from './transcript-folder.js';
import {recordOf} from './usage-records.js';
import {workstreamsWithFinish, workstreamsWithUsage} from './workstream.js';

type RunEventData = NonNullable<RunEvent['data']>;

type EventDataKind = RunEventData['kind'];
type EventDataHandler = (state: CoreState, event: RunEvent, sequence: number) => CoreState;
interface EventDataHandlers {
  readonly execution: EventDataHandler;
  readonly run: EventDataHandler;
}

const ignoreEventData: EventDataHandler = state => state;
const ignoredEventData: EventDataHandlers = {
  execution: ignoreEventData,
  run: ignoreEventData,
};

function runEventData(run: EventDataHandler): EventDataHandlers {
  return {execution: ignoreEventData, run};
}

function executionEventData(
  execution: EventDataHandler,
  run: EventDataHandler = ignoreEventData,
): EventDataHandlers {
  return {execution, run};
}

/** Applies execution bookkeeping before chat events branch to their transcript. */
export function applyExecutionEventData(
  state: CoreState,
  event: RunEvent,
  sequence: number,
  handlers: EventDataHandlers | null | undefined,
): CoreState {
  if (handlers === null) {
    return cloneCoreStateWith(state, {unknownEventKinds: state.unknownEventKinds + 1});
  }
  return handlers?.execution(state, event, sequence) ?? state;
}

/** Applies facts owned by the run after chat-session events have branched. */
export function applyRunEventData(
  state: CoreState,
  event: RunEvent,
  sequence: number,
  handlers: EventDataHandlers | null | undefined,
): CoreState {
  return handlers?.run(state, event, sequence) ?? applyTerminalExecutionStatus(state, event);
}

function composeEventDataHandlers(...handlers: readonly EventDataHandler[]): EventDataHandler {
  return (state, event, sequence) =>
    handlers.reduce((current, handler) => handler(current, event, sequence), state);
}

/**
 * One exhaustive dispatch table for every generated event-data kind.
 *
 * `satisfies` makes a protocol kind addition a compile error here. At runtime
 * #869 permits a newer server's unknown string through the wire boundary, so
 * `eventDataHandlers` reports that separate forward-compatibility case.
 */
const EVENT_DATA_HANDLERS = {
  chat: ignoredEventData,
  chat_thread_created: ignoredEventData,
  invocation_started: ignoredEventData,
  invocation_finished: ignoredEventData,
  agent_execution_started: executionEventData(
    applyAgentExecutionStartedData,
    reconcileStartedExecutionStatus,
  ),
  agent_execution_activity_changed: executionEventData(applyAgentExecutionActivityData),
  agent_execution_finished: executionEventData(
    applyAgentExecutionFinishedData,
    removeFinishedExecutionStatus,
  ),
  output: ignoredEventData,
  server_ready: ignoredEventData,
  run_started: ignoredEventData,
  run_startup_progress: runEventData(applyRunStartupProgressData),
  run_failed: runEventData(applyTerminalExecutionStatus),
  run_interrupted: runEventData(applyTerminalExecutionStatus),
  run_status_changed: runEventData(
    composeEventDataHandlers(applyTerminalExecutionStatus, applyRunStatusData),
  ),
  experiments_changed: runEventData(applyExperimentsChangedData),
  iterations_changed: ignoredEventData,
  configuration_failed: runEventData(applyTerminalExecutionStatus),
  phase: ignoredEventData,
  agent_output_chunk: runEventData(applyAgentStatusData),
  subprocess_output: ignoredEventData,
  judge_result: ignoredEventData,
  benchmark_result: runEventData(applyBenchmarkData),
  round_finished: ignoredEventData,
  tool_call: runEventData(composeEventDataHandlers(applyAgentStatusData, applyTypedToolData)),
  tool_result: runEventData(applyTypedToolData),
  // A steer proposal belongs to a chat thread, not the run's core state. The chat fold reads it
  steer_proposed: executionEventData(applyChatProposal),
  steer_delivery: executionEventData(applySteerDelivery),
  todo_update: runEventData(applyTodoData),
  usage_update: runEventData(applyUsageData),
  rate_limit_update: ignoredEventData,
  quota_paused: runEventData(applyQuotaData),
  quota_resumed: runEventData(applyQuotaData),
  quota_abandoned: runEventData(applyQuotaData),
  quota_waiting: runEventData(applyQuotaData),
  quota_wait_cancelled: runEventData(applyQuotaData),
  provider_switched: runEventData(applyQuotaData),
  gate_started: ignoredEventData,
  gate_finished: runEventData(applyBenchmarkData),
  workspace_snapshot: ignoredEventData,
  run_configured: ignoredEventData,
  framework_warning: ignoredEventData,
  measurement_compared: runEventData(applyMeasurementComparedData),
  winner_selected: runEventData(applyWinnerSelectedData),
  workstream_finished: runEventData(applyWorkstreamFinishedData),
  turn_usage_recorded: runEventData(applyTurnUsageData),
  // The run trace is its own fold (`@vibesys/run-state/trace`), not part of the run's core state.
  span_started: ignoredEventData,
  span_updated: ignoredEventData,
  span_ended: ignoredEventData,
  run_ending: ignoredEventData,
} satisfies Record<EventDataKind, EventDataHandlers>;

export function eventDataHandlers(data: RunEvent['data']): EventDataHandlers | null | undefined {
  if (data === undefined || data === null) return undefined;
  return isKnownEventDataKind(data.kind) ? EVENT_DATA_HANDLERS[data.kind] : null;
}

function isKnownEventDataKind(kind: string): kind is EventDataKind {
  return Object.hasOwn(EVENT_DATA_HANDLERS, kind);
}

function applyTypedToolData(state: CoreState): CoreState {
  return state.typedToolEvents ? state : cloneCoreStateWith(state, {typedToolEvents: true});
}

function applyTodoData(state: CoreState, event: RunEvent): CoreState {
  return cloneCoreStateWith(state, {todos: updateTodos(state.todos, event)});
}

function applyUsageData(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (data?.kind !== 'usage_update') return state;
  return cloneCoreStateWith(state, {
    usage: {
      inputTokens: data.input_tokens,
      contextWindow: data.context_window ?? null,
      model: data.model ?? null,
    },
  });
}

function applyQuotaData(state: CoreState, event: RunEvent, sequence: number): CoreState {
  const quota = quotaFromEvent(state.quota, event, sequence);
  const stop = quotaStopFromEvent(event, sequence);
  if (quota === state.quota && stop === null) return state;
  return cloneCoreStateWith(state, {
    quota,
    ...(stop === null ? {} : {quotaStops: [...state.quotaStops, stop]}),
  });
}

/** The stop a `quota_resumed` or `quota_abandoned` event ends, when it held a turn for some time. */
function quotaStopFromEvent(event: RunEvent, sequence: number): QuotaStop | null {
  const data = event.data;
  if (data?.kind !== 'quota_resumed' && data?.kind !== 'quota_abandoned') return null;
  const waitedSeconds = data.waited_seconds ?? 0;
  if (!(waitedSeconds > 0)) return null;
  return {
    sequence,
    provider: data.provider,
    condition: data.condition ?? null,
    outcome: data.kind === 'quota_resumed' ? 'resumed' : 'abandoned',
    waitedSeconds,
    endedAt: event.timestamp,
  };
}

function applyBenchmarkData(state: CoreState, event: RunEvent, sequence: number): CoreState {
  const benchmark = benchmarkRecordFromEvent(event, sequence);
  return benchmark === null
    ? state
    : cloneCoreStateWith(state, {benchmarks: [...state.benchmarks, benchmark]});
}

function applyWinnerSelectedData(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (data?.kind !== 'winner_selected') return state;
  return cloneCoreStateWith(state, {
    winner: {
      selection: data.selection,
      revision: data.revision,
      settlementId: data.settlement_id ?? null,
    },
  });
}

function applyMeasurementComparedData(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (data?.kind !== 'measurement_compared') return state;
  return cloneCoreStateWith(state, {
    comparisons: {
      ...state.comparisons,
      [data.candidate_commit]: {
        verdict: data.verdict,
        ratio: data.ratio,
        metric: data.metric,
        candidateValue: data.candidate_value,
        baseValue: data.base_value,
        baseCommit: data.base_commit,
        candidateSamples: data.candidate_samples,
        baseSamples: data.base_samples,
      },
    },
  });
}

function applyWorkstreamFinishedData(
  state: CoreState,
  event: RunEvent,
  sequence: number,
): CoreState {
  const data = event.data;
  if (data?.kind !== 'workstream_finished') return state;
  const workstreams = workstreamsWithFinish(state.workstreams, event, data, sequence);
  return workstreams === state.workstreams ? state : cloneCoreStateWith(state, {workstreams});
}

function applyTurnUsageData(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (data?.kind !== 'turn_usage_recorded') return state;
  const record = recordOf(state.executionStarts, event);
  return cloneCoreStateWith(state, {
    workstreams: workstreamsWithUsage(state.workstreams, event, data),
    tokens: ledgerWith(state.tokens, data),
    ...(record === null ? {} : {usageRecords: [...state.usageRecords, record]}),
  });
}

function applyExperimentsChangedData(
  state: CoreState,
  _event: RunEvent,
  sequence: number,
): CoreState {
  return cloneCoreStateWith(state, {experimentsRevision: sequence});
}

function applyRunStartupProgressData(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (data?.kind !== 'run_startup_progress') return state;
  const progress = data.progress;
  const previous = state.startup;
  const sameStage = previous !== null && previous.stage === data.stage;
  const stepState = data.state ?? 'running';
  const took =
    stepState === 'finished' && data.elapsed_seconds != null
      ? {
          ...state.startupTook,
          [data.stage]: (state.startupTook[data.stage] ?? 0) + data.elapsed_seconds,
        }
      : state.startupTook;
  return cloneCoreStateWith(state, {
    startupTook: took,
    startup: {
      stage: data.stage,
      state: stepState,
      condition: data.condition ?? null,
      elapsedSeconds: data.elapsed_seconds ?? null,
      stageSince: sameStage ? previous.stageSince : event.timestamp,
      lastActivityAt: event.timestamp,
      // A wait or a finish carries no step of its own: the stage's last step stays the latest.
      progress:
        progress != null
          ? {done: progress.done, total: progress.total ?? null, label: progress.label ?? ''}
          : stepState !== 'running' && sameStage
            ? previous.progress
            : null,
    },
  });
}

function applyRunStatusData(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (data?.kind !== 'run_status_changed') return state;
  // The backend owns the run's lifecycle and publishes every move through it,
  // so the projection folds the status it is told rather than inferring one.
  return applyRunStatus(state, data.status);
}

/** The pause a `quota_paused` event on a named limit (`condition`) sets, waiting on nothing yet. */
function pauseFromEvent(
  data: Extract<RunEventData, {kind: 'quota_paused'}>,
  condition: QuotaPause['condition'],
): QuotaPause {
  return {
    provider: data.provider,
    condition,
    detail: data.detail,
    resetsAt: data.resets_at ?? null,
    resumesAt: data.resumes_at ?? null,
    policy: data.policy ?? null,
    fallback:
      data.fallback_provider == null
        ? null
        : {provider: data.fallback_provider, model: data.fallback_model ?? null},
    waiting: null,
  };
}

/**
 * The quota state after `event`: a pause sets it, any event that settles the stop clears it.
 * `provider_switched` additionally records what it switched, in `lastSwitch`: that fact is
 * sticky, so every branch carries the previous one forward rather than dropping it.
 *
 * An outage (`unavailable`) never pauses the run: only the stopped turn waits, while other
 * turns go on and may hold a real pause. So its events neither set nor settle the pause.
 */
function quotaFromEvent(quota: QuotaState, event: RunEvent, sequence: number): QuotaState {
  const data = event.data;
  switch (data?.kind) {
    case 'quota_paused':
      if (data.condition === 'unavailable') return quota;
      return {...quota, sequence, pause: pauseFromEvent(data, data.condition)};
    case 'quota_waiting':
      // A wait belongs to the pause it waits out; one with no pause folded has nothing to show.
      return quota.pause === null
        ? quota
        : {
            ...quota,
            sequence,
            pause: {...quota.pause, waiting: {resumesAt: data.resumes_at ?? null}},
          };
    case 'quota_wait_cancelled':
      return quota.pause === null
        ? quota
        : {...quota, sequence, pause: {...quota.pause, waiting: null}};
    case 'quota_resumed':
    case 'quota_abandoned':
      if (data.condition === 'unavailable') return quota;
      return {...quota, sequence, pause: null};
    case 'provider_switched':
      return {
        ...quota,
        sequence,
        pause: null,
        lastSwitch: {
          sequence,
          fromProvider: data.from_provider,
          toProvider: data.to_provider,
          toModel: data.to_model,
          reason: data.reason,
          detail: data.detail,
        },
      };
    default:
      return quota;
  }
}

export function applyRunTranscript(
  state: CoreState,
  event: RunEvent,
  folder: TranscriptFolder | null,
): CoreState {
  const data = event.data;
  const legacyToolChunk =
    data?.kind === 'agent_output_chunk' && data.channel === 'tool' && state.typedToolEvents;
  if (legacyToolChunk) return state;
  const entry = eventToTranscriptEntry(event);
  if (entry === null) return state;
  if (folder === null)
    (state as MutableCoreState).transcript = appendTranscript(state.transcript, entry);
  else folder.buffer(RUN_TRANSCRIPT, state.transcript).append(entry);
  return state;
}

export function applyRunLifecycle(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (event.type === 'run_started') {
    const mutable = state as MutableCoreState;
    mutable.status = 'running';
    if (data?.kind === 'run_started') mutable.maxRounds = data.max_rounds ?? null;
  }
  if (event.type === 'configuration_failed') return terminate(state, 'failed');
  if (event.type === 'run_finished') return terminate(state, 'completed');
  if (event.type === 'run_failed') return terminate(state, 'failed');
  if (event.type === 'run_interrupted') return terminate(state, 'interrupted');
  return state;
}

/** Folds one backend-published status, ending the run when that status has. */
function applyRunStatus(state: CoreState, status: CoreRunStatus): CoreState {
  const ended = endedRunStatus(status);
  return ended === null ? cloneCoreStateWith(state, {status}) : terminate(state, ended);
}

/**
 * Ends the run, unless it already has.
 *
 * A recorded run_interrupted is followed by a run_failed for the same
 * boundary (the backend's coarse RunStatus has no `interrupted` member, so it
 * also reports the generic failure for anything that only understands that
 * one); folding both must not let the second, less specific event overwrite
 * the first. `closePhase` in `run-map.ts` holds the same line for per-phase
 * status, by leaving an already-closed phase alone. This is the run-level
 * counterpart: once a status a run never leaves is written, `terminate` no
 * longer has anything to write.
 */
function terminate(state: CoreState, status: EndedRunStatus): CoreState {
  if (hasRunEnded(state)) return state;
  return cloneCoreStateWith(state, {status, activeExecutions: {}});
}
