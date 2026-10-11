interface AgentTimingInterval {
  readonly startedAt: string;
  readonly finishedAt: string;
}

export interface RoundTimingState {
  readonly agentIntervals?: readonly AgentTimingInterval[];
  readonly activeAgentStarts?: Readonly<Record<string, string>>;
}

export interface AgentTimingEvent {
  agent_kind?: string | null;
  invocation_id?: string | null;
  execution_id?: string | null;
  timestamp: string;
  sequence?: number;
  type?: string;
}

export function startAgentTiming<T extends RoundTimingState>(state: T, event: AgentTimingEvent): T {
  const key = timingKey(event);
  if (key === null) return state;
  return {
    ...state,
    activeAgentStarts: {...(state.activeAgentStarts ?? {}), [key]: event.timestamp},
  };
}

export function finishAgentTiming<T extends RoundTimingState>(
  state: T,
  event: AgentTimingEvent,
): T {
  const exactKey = timingKey(event);
  if (exactKey === null || !event.agent_kind) return state;
  const activeAgentStarts = {...(state.activeAgentStarts ?? {})};
  const activeKey = findActiveTimingKey(activeAgentStarts, event, exactKey);
  if (activeKey === null) return state;
  const startedAt = activeAgentStarts[activeKey];
  if (startedAt === undefined) return state;
  delete activeAgentStarts[activeKey];
  return {
    ...state,
    agentIntervals: [...(state.agentIntervals ?? []), {startedAt, finishedAt: event.timestamp}],
    activeAgentStarts,
  };
}

export function closeActiveAgentTimings<T extends RoundTimingState>(
  state: T,
  timestamp: string,
  _sequence: number | null = null,
): T {
  const activeIntervals = Object.values(state.activeAgentStarts ?? {}).map(startedAt => ({
    startedAt,
    finishedAt: timestamp,
  }));
  return {
    ...state,
    agentIntervals: [...(state.agentIntervals ?? []), ...activeIntervals],
    activeAgentStarts: {},
  };
}

/** The caller supplies time so this selector stays deterministic. */
export function activeTimingElapsedMs(state: RoundTimingState, now: Date): number {
  const finishedAt = now.toISOString();
  const activeIntervals = Object.values(state.activeAgentStarts ?? {}).map(startedAt => ({
    startedAt,
    finishedAt,
  }));
  return intervalUnionElapsedMs([...(state.agentIntervals ?? []), ...activeIntervals]);
}

export function hasActiveAgentTiming(state: RoundTimingState): boolean {
  return Object.keys(state.activeAgentStarts ?? {}).length > 0;
}

function timingKey(event: AgentTimingEvent): string | null {
  if (!event.agent_kind) return null;
  return `${event.agent_kind}:${event.execution_id ?? event.invocation_id ?? ''}`;
}

function findActiveTimingKey(
  activeAgentStarts: Record<string, string>,
  event: AgentTimingEvent,
  exactKey: string,
): string | null {
  if (activeAgentStarts[exactKey] !== undefined) return exactKey;
  if (!event.agent_kind) return null;
  const prefix = `${event.agent_kind}:`;
  const candidates = Object.entries(activeAgentStarts)
    .filter(([key]) => key.startsWith(prefix))
    .sort(([, left], [, right]) => new Date(left).getTime() - new Date(right).getTime());
  return candidates[0]?.[0] ?? null;
}

function intervalUnionElapsedMs(intervals: readonly AgentTimingInterval[]): number {
  const ranges = intervals
    .map(interval => ({
      start: new Date(interval.startedAt).getTime(),
      end: new Date(interval.finishedAt).getTime(),
    }))
    .filter(range => !Number.isNaN(range.start) && !Number.isNaN(range.end))
    .map(range => ({
      start: Math.min(range.start, range.end),
      end: Math.max(range.start, range.end),
    }))
    .sort((left, right) => left.start - right.start);
  let elapsedMs = 0;
  let current: {start: number; end: number} | null = null;
  for (const range of ranges) {
    if (current === null) {
      current = range;
      continue;
    }
    if (range.start <= current.end) {
      current.end = Math.max(current.end, range.end);
      continue;
    }
    elapsedMs += current.end - current.start;
    current = range;
  }
  if (current !== null) elapsedMs += current.end - current.start;
  return elapsedMs;
}
