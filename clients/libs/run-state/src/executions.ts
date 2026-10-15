/**
 * Agent executions in the run fold: the active executions, their status, todos, and the
 * reconciliation against a snapshot's checkpoint.
 */
import type {RunEvent} from '@vibesys/contracts';
import {
  applyExecutionStatus,
  applyExecutionStatusUsage,
  reconcileExecutionStatuses,
  removeExecutionStatus,
} from './execution-status.js';
import {roundKeyFor, roundNumberFor, sameRoundKey} from './round-key.js';
import {
  type ActiveAgentExecution,
  type ActiveExecutionCheckpoint,
  type CoreState,
  cloneCoreStateWith,
  type ExecutionTodos,
  endedRunStatus,
  publishCoreState,
} from './state.js';

/** Checkpoints reconcile liveness without changing the event replay cursor. */
export function reconcileActiveExecutions(
  state: CoreState,
  executions: ActiveExecutionCheckpoint,
  throughSequence?: number,
): CoreState {
  if (throughSequence !== undefined && throughSequence < state.sequence) return state;
  const activeExecutions = activeExecutionsFromCheckpoint(executions);
  const next = cloneCoreStateWith(state, {
    activeExecutions,
    executionStatuses: reconcileExecutionStatuses(state.executionStatuses, activeExecutions),
  });
  return publishCoreState(next);
}

/**
 * Whether `event` is the run reaching a status it never leaves.
 *
 * The same fact `applyRunLifecycle` and `applyRunStatus` route to `terminate`,
 * asked one stage earlier so the per-execution status map closes out with the
 * rest of the run. Statuses are the reason this is not just a list of event
 * types: an operator `/stop` ends the run through `run_status_changed` alone,
 * with no run-scoped terminal event after it.
 *
 * `run-map.ts`'s `runClosingStatus` is deliberately narrower: it answers what
 * to write onto work that was still open, and `completed` and `failed` already
 * have a terminal event that owns that. Clearing a status map the run has
 * finished with has no such owner and no ordering hazard.
 */
function endsRun(event: RunEvent): boolean {
  switch (event.type) {
    case 'run_finished':
    case 'run_failed':
    case 'run_interrupted':
    case 'configuration_failed':
      return true;
    default: {
      const data = event.data;
      return data?.kind === 'run_status_changed' && endedRunStatus(data.status) !== null;
    }
  }
}

export function activeExecutionsFromCheckpoint(
  executions: ActiveExecutionCheckpoint,
): Record<string, ActiveAgentExecution> {
  return Object.fromEntries(
    executions.map(execution => [execution.execution_id, activeExecutionFromCheckpoint(execution)]),
  );
}

function activeExecutionFromCheckpoint(
  execution: ActiveExecutionCheckpoint[number],
): ActiveAgentExecution {
  const roundKey = roundKeyFor(execution);
  return {
    executionId: execution.execution_id,
    agentKind: execution.agent_kind,
    roundLabel: execution.round_label ?? null,
    roundNumber: roundNumberFor(roundKey),
    roundKey,
    stage: execution.stage,
    attempt: execution.attempt ?? null,
    assignment: execution.assignment,
    startedAt: execution.started_at,
    activity: {
      mode: execution.activity.mode,
      summary: execution.activity.summary,
      tool: execution.activity.tool ?? null,
    },
    provider: execution.provider ?? null,
    model: execution.model ?? null,
  };
}

export function applyAgentExecutionStartedData(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.execution_id;
  const data = event.data;
  if (executionId == null || data?.kind !== 'agent_execution_started') return state;
  const roundKey = roundKeyFor(event);
  return cloneCoreStateWith(state, {
    activeExecutions: {
      ...state.activeExecutions,
      [executionId]: {
        executionId,
        agentKind: event.agent_kind ?? 'agent',
        roundLabel: event.round_label ?? null,
        roundNumber: roundNumberFor(roundKey),
        roundKey,
        stage: data.stage,
        attempt: data.attempt ?? null,
        assignment: data.user_prompt ?? '',
        startedAt: event.timestamp,
        activity: {
          mode: data.activity.mode,
          summary: data.activity.summary,
          tool: data.activity.tool ?? null,
        },
        provider: data.provider ?? null,
        model: data.model ?? null,
      },
    },
  });
}

export function applyAgentExecutionActivityData(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.execution_id;
  const data = event.data;
  if (executionId == null || data?.kind !== 'agent_execution_activity_changed') return state;
  const current = state.activeExecutions[executionId];
  if (current === undefined) return state;
  return cloneCoreStateWith(state, {
    activeExecutions: {
      ...state.activeExecutions,
      [executionId]: {
        ...current,
        activity: {mode: data.mode, summary: data.summary, tool: data.tool ?? null},
      },
    },
  });
}

export function applyAgentExecutionFinishedData(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.execution_id;
  if (executionId == null || event.data?.kind !== 'agent_execution_finished') return state;
  const {[executionId]: _finished, ...remaining} = state.activeExecutions;
  return cloneCoreStateWith(state, {activeExecutions: remaining});
}

export function reconcileStartedExecutionStatus(state: CoreState): CoreState {
  const executionStatuses = reconcileExecutionStatuses(
    state.executionStatuses,
    state.activeExecutions,
  );
  return executionStatuses === state.executionStatuses
    ? state
    : cloneCoreStateWith(state, {executionStatuses});
}

export function removeFinishedExecutionStatus(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.execution_id;
  if (executionId == null) return state;
  const executionStatuses = removeExecutionStatus(state.executionStatuses, executionId);
  return executionStatuses === state.executionStatuses
    ? state
    : cloneCoreStateWith(state, {executionStatuses});
}

export function applyTerminalExecutionStatus(state: CoreState, event: RunEvent): CoreState {
  return endsRun(event) && Object.keys(state.executionStatuses).length > 0
    ? cloneCoreStateWith(state, {executionStatuses: {}})
    : state;
}

export function applyAgentStatusData(state: CoreState, event: RunEvent): CoreState {
  const previousStatuses = state.executionStatuses;
  const reconciled =
    Object.keys(state.activeExecutions).length === 0
      ? previousStatuses
      : reconcileExecutionStatuses(previousStatuses, state.activeExecutions);
  const executionStatuses = applyExecutionStatus(reconciled, event);
  const usage = applyExecutionStatusUsage(state.usage, previousStatuses, executionStatuses, event);
  if (executionStatuses === previousStatuses && usage === state.usage) return state;
  return cloneCoreStateWith(state, {executionStatuses, usage});
}

export function updateTodos(
  previous: readonly ExecutionTodos[],
  event: RunEvent,
): readonly ExecutionTodos[] {
  const data = event.data;
  if (data?.kind !== 'todo_update') return previous;
  const agentKind = event.agent_kind ?? null;
  const roundKey = roundKeyFor(event);
  const roundNumber = roundNumberFor(roundKey);
  const executionId = event.execution_id ?? event.invocation_id ?? null;
  const retained = previous.filter(item =>
    executionId === null
      ? item.executionId != null ||
        item.agentKind !== agentKind ||
        !sameRoundKey(item.roundKey, roundKey)
      : item.executionId !== executionId,
  );
  return [
    ...retained,
    {
      executionId,
      agentKind,
      roundNumber,
      roundKey,
      items: (data.todos ?? []).map(todo => ({
        content: String(todo.content),
        status: String(todo.status),
      })),
    },
  ].slice(-100);
}
