import type {AgentStatusData, RunEvent} from '@vibesys/backend-client';

export interface ExecutionStatus {
  readonly executionId: string;
  readonly sequence: number;
  readonly observedAt: string;
  readonly progress: string | null;
  readonly agentLabel: string | null;
  readonly elapsedSeconds: number | null;
  readonly inputTokens: number | null;
  readonly contextWindow: number | null;
}

type StatusMap = Record<string, ExecutionStatus>;
type ActiveExecutionMap = Record<string, {startedAt: string}>;

interface ExecutionUsage {
  inputTokens: number;
  contextWindow: number | null;
  model: string | null;
}

/** Returns status only when it belongs to the active execution generation. */
export function executionStatusFor(
  statuses: Readonly<StatusMap>,
  execution: Readonly<{executionId: string; startedAt: string}>,
): ExecutionStatus | undefined {
  const status = statuses[execution.executionId];
  if (status === undefined) return undefined;
  return Date.parse(status.observedAt) >= Date.parse(execution.startedAt) ? status : undefined;
}

/** Applies one status-bearing presentation event to its execution. */
export function applyExecutionStatus(statuses: StatusMap, event: RunEvent): StatusMap {
  const executionId = executionIdentity(event);
  const data = event.data;
  const status =
    data?.kind === 'agent_output_chunk' || data?.kind === 'tool_call' ? data.status : null;
  if (executionId == null || status == null) return statuses;
  const sequence = event.sequence ?? 0;
  const current = statuses[executionId];
  if (
    current !== undefined &&
    (current.sequence > sequence ||
      (sequence === 0 ? current.sequence > 0 : current.sequence === sequence))
  ) {
    return statuses;
  }
  return {
    ...statuses,
    [executionId]: {
      executionId,
      sequence,
      observedAt: event.timestamp,
      progress: status.progress ?? current?.progress ?? null,
      agentLabel: status.agent_label ?? current?.agentLabel ?? null,
      elapsedSeconds: status.elapsed_seconds ?? current?.elapsedSeconds ?? null,
      inputTokens: reportedInputTokens(status.input_tokens) ?? current?.inputTokens ?? null,
      contextWindow: status.context_window ?? current?.contextWindow ?? null,
    },
  };
}

/** Updates global usage only from the status update accepted for this execution. */
export function applyExecutionStatusUsage(
  current: ExecutionUsage | null,
  previousStatuses: StatusMap,
  nextStatuses: StatusMap,
  event: RunEvent,
): ExecutionUsage | null {
  const raw = executionStatusData(event);
  if (raw === null) return current;
  const rawInputTokens = reportedInputTokens(raw.input_tokens);
  if (rawInputTokens == null) return current;
  const executionId = executionIdentity(event);
  if (executionId === null) {
    return {
      inputTokens: rawInputTokens,
      contextWindow: raw.context_window ?? current?.contextWindow ?? null,
      model: current?.model ?? null,
    };
  }
  const status = nextStatuses[executionId];
  if (
    status === undefined ||
    status === previousStatuses[executionId] ||
    status.inputTokens === null
  ) {
    return current;
  }
  return {
    inputTokens: status.inputTokens,
    contextWindow: status.contextWindow,
    model: current?.model ?? null,
  };
}

/** Drops status for an execution that is no longer active. */
export function removeExecutionStatus(statuses: StatusMap, executionId: string): StatusMap {
  if (statuses[executionId] === undefined) return statuses;
  const {[executionId]: _finished, ...remaining} = statuses;
  return remaining;
}

/** Reconciles status with the backend's authoritative active-execution checkpoint. */
export function reconcileExecutionStatuses(
  statuses: StatusMap,
  active: ActiveExecutionMap,
): StatusMap {
  const entries = Object.entries(statuses);
  const retained = entries.filter(([executionId, status]) => {
    const execution = active[executionId];
    return (
      execution !== undefined && Date.parse(status.observedAt) >= Date.parse(execution.startedAt)
    );
  });
  return retained.length === entries.length ? statuses : Object.fromEntries(retained);
}

/** Reads structured status from the two protocol events that carry it. */
function executionStatusData(event: RunEvent): AgentStatusData | null {
  const data = event.data;
  return data?.kind === 'agent_output_chunk' || data?.kind === 'tool_call'
    ? (data.status ?? null)
    : null;
}

function executionIdentity(event: RunEvent): string | null {
  return event.execution_id ?? event.invocation_id ?? null;
}

/**
 * A status reports usable context pressure only once its token count is
 * positive. The backend seeds `input_tokens` at 0 and emits that before an
 * agent's first completion, treating a zero (or missing) count as "no update"
 * (see agents/callbacks.py). Mirror that here so a 0 leaves the prior reading
 * intact instead of overwriting a real count and blanking the meter.
 */
function reportedInputTokens(value: number | null | undefined): number | null {
  return value != null && value > 0 ? value : null;
}
