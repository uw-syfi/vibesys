import type {AgentStatusData, RunEvent} from '@vibesys/backend-client';

export interface ExecutionStatus {
  executionId: string;
  sequence: number;
  observedAt: string;
  progress: string | null;
  agentLabel: string | null;
  elapsedSeconds: number | null;
  inputTokens: number | null;
  contextWindow: number | null;
}

type StatusMap = Record<string, ExecutionStatus>;
type ActiveExecutionMap = Record<string, {startedAt: string}>;

interface ExecutionUsage {
  inputTokens: number;
  contextWindow: number | null;
  model: string | null;
}

interface StatusUsageSource {
  executionId: string;
  sequence: number;
  generationStartedAt: string | null;
  status: ExecutionStatus;
}

const statusUsageSources = new WeakMap<object, StatusUsageSource>();
const unresolvedStatusUsageModels = new WeakSet<object>();

/** Returns status only when it belongs to the active execution generation. */
export function executionStatusFor(
  statuses: StatusMap,
  execution: {executionId: string; startedAt: string},
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
      inputTokens: status.input_tokens ?? current?.inputTokens ?? null,
      contextWindow: status.context_window ?? current?.contextWindow ?? null,
    },
  };
}

/** Updates global usage only from the status update accepted for this execution. */
export function applyExecutionStatusUsage(
  current: ExecutionUsage | null,
  previousStatuses: StatusMap,
  nextStatuses: StatusMap,
  active: ActiveExecutionMap,
  event: RunEvent,
): ExecutionUsage | null {
  const raw = executionStatusData(event);
  if (raw?.input_tokens == null) return current;
  const executionId = executionIdentity(event);
  if (executionId === null) {
    const usage: ExecutionUsage = {
      inputTokens: raw.input_tokens,
      contextWindow: raw.context_window ?? current?.contextWindow ?? null,
      model: current?.model ?? null,
    };
    if (needsModelBackfill(current)) unresolvedStatusUsageModels.add(usage);
    return usage;
  }
  const status = nextStatuses[executionId];
  if (
    status === undefined ||
    status === previousStatuses[executionId] ||
    status.inputTokens === null
  ) {
    return current;
  }
  const usage: ExecutionUsage = {
    inputTokens: status.inputTokens,
    contextWindow: status.contextWindow,
    model: current?.model ?? null,
  };
  statusUsageSources.set(usage, {
    executionId,
    sequence: status.sequence,
    generationStartedAt: active[executionId]?.startedAt ?? null,
    status,
  });
  if (needsModelBackfill(current)) unresolvedStatusUsageModels.add(usage);
  return usage;
}

/** Restores fields supplied by an older status when a partial tail status owns usage. */
export function mergeExecutionStatusUsagePrefix(
  usage: ExecutionUsage | null,
  statuses: StatusMap,
  olderStatuses: StatusMap,
  prefixEvents: readonly RunEvent[],
  olderUsage: ExecutionUsage | null = null,
): ExecutionUsage | null {
  if (usage === null) return null;
  const olderUsageCandidate = olderUsage === usage ? null : olderUsage;
  const source = statusUsageSources.get(usage);
  if (source === undefined) {
    if (!unresolvedStatusUsageModels.has(usage) || olderUsageCandidate === null) return usage;
    const merged = {...usage, model: olderUsageCandidate.model};
    if (merged.model === null && unresolvedStatusUsageModels.has(olderUsageCandidate)) {
      unresolvedStatusUsageModels.add(merged);
    }
    return merged;
  }
  const generationStartedAt =
    source.generationStartedAt ??
    latestExecutionStart(prefixEvents, source.executionId, source.sequence);
  const older = olderStatuses[source.executionId];
  const eligibleOlder =
    older !== undefined &&
    (generationStartedAt === null ||
      Date.parse(older.observedAt) >= Date.parse(generationStartedAt))
      ? older
      : undefined;
  const projected = statuses[source.executionId];
  const status =
    projected !== undefined && projected.sequence === source.sequence
      ? projected
      : mergeStatus(eligibleOlder, source.status);
  if (status.inputTokens === null) return usage;
  let model = usage.model;
  let modelNeedsBackfill = unresolvedStatusUsageModels.has(usage);
  if (modelNeedsBackfill && olderUsageCandidate !== null) {
    model = olderUsageCandidate.model;
    modelNeedsBackfill = model === null && unresolvedStatusUsageModels.has(olderUsageCandidate);
  }
  const merged: ExecutionUsage = {
    inputTokens: status.inputTokens,
    contextWindow: status.contextWindow,
    model,
  };
  statusUsageSources.set(merged, {...source, generationStartedAt, status});
  if (modelNeedsBackfill) unresolvedStatusUsageModels.add(merged);
  return merged;
}

function needsModelBackfill(current: ExecutionUsage | null): boolean {
  return current === null || unresolvedStatusUsageModels.has(current);
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

/** Merges an older prefix under a newer suffix, retaining each execution's latest status. */
export function mergeExecutionStatusesPrefix(
  older: StatusMap,
  newer: StatusMap,
  active: ActiveExecutionMap,
): StatusMap {
  const eligibleOlder = reconcileExecutionStatuses(older, active);
  const eligibleNewer = reconcileExecutionStatuses(newer, active);
  const merged = {...eligibleOlder};
  for (const [executionId, status] of Object.entries(eligibleNewer)) {
    const previous = merged[executionId];
    if (
      previous === undefined ||
      status.sequence > previous.sequence ||
      (status.sequence === 0 && previous.sequence === 0)
    ) {
      merged[executionId] = mergeStatus(previous, status);
    }
  }
  return merged;
}

function mergeStatus(older: ExecutionStatus | undefined, newer: ExecutionStatus): ExecutionStatus {
  if (older === undefined) return newer;
  return {
    ...newer,
    progress: newer.progress ?? older.progress,
    agentLabel: newer.agentLabel ?? older.agentLabel,
    elapsedSeconds: newer.elapsedSeconds ?? older.elapsedSeconds,
    inputTokens: newer.inputTokens ?? older.inputTokens,
    contextWindow: newer.contextWindow ?? older.contextWindow,
  };
}

/** Reads structured status from the two protocol events that carry it. */
export function executionStatusData(event: RunEvent): AgentStatusData | null {
  const data = event.data;
  return data?.kind === 'agent_output_chunk' || data?.kind === 'tool_call'
    ? (data.status ?? null)
    : null;
}

function executionIdentity(event: RunEvent): string | null {
  return event.execution_id ?? event.invocation_id ?? null;
}

function latestExecutionStart(
  events: readonly RunEvent[],
  executionId: string,
  throughSequence: number,
): string | null {
  let startedAt: string | null = null;
  let latestSequence = -1;
  for (const event of events) {
    if (event.execution_id !== executionId || event.data?.kind !== 'agent_execution_started') {
      continue;
    }
    const sequence = event.sequence ?? 0;
    if (throughSequence > 0 && sequence > throughSequence) continue;
    if (sequence >= latestSequence) {
      latestSequence = sequence;
      startedAt = event.timestamp;
    }
  }
  return startedAt;
}
