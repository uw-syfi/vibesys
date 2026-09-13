export interface AgentTimingInterval {
  startedAt: string;
  finishedAt: string;
}

interface TimingFinish {
  key: string;
  agentKind: string;
  finishedAt: string;
  sequence: number | null;
  compatibility: boolean;
}

interface TimingCloseout {
  finishedAt: string;
  sequence: number | null;
}

interface TimingIntervalProvenance {
  startKey: string;
  startSequence: number | null;
  startCompatibility: boolean;
  finishKey: string;
  agentKind: string;
  finishSequence: number | null;
  finishCompatibility: boolean;
}

/** Internal replay provenance used to join timing facts across prefix boundaries. */
interface AgentTimingProvenance {
  activeStarts: Record<string, TimingStartProvenance>;
  intervals: Array<TimingIntervalProvenance | null>;
  unmatchedFinishes: TimingFinish[];
  closeouts: TimingCloseout[];
}

interface TimingStartProvenance {
  sequence: number | null;
  compatibility: boolean;
}

export interface RoundTimingState {
  agentIntervals?: AgentTimingInterval[];
  activeAgentStarts?: Record<string, string>;
}

export interface AgentTimingEvent {
  agent_kind?: string | null;
  invocation_id?: string | null;
  execution_id?: string | null;
  timestamp: string;
  sequence?: number;
  type?: string;
}

const timingProvenances = new WeakMap<object, AgentTimingProvenance>();

export function startAgentTiming<T extends RoundTimingState>(state: T, event: AgentTimingEvent): T {
  const key = timingKey(event);
  if (key === null) return state;
  const provenance = timingProvenance(state);
  const next = {
    ...state,
    activeAgentStarts: {...(state.activeAgentStarts ?? {}), [key]: event.timestamp},
  };
  recordTimingProvenance(next, {
    ...provenance,
    activeStarts: {
      ...provenance.activeStarts,
      [key]: {
        sequence: event.sequence ?? null,
        compatibility: event.type === 'phase_started',
      },
    },
  });
  return next;
}

export function finishAgentTiming<T extends RoundTimingState>(
  state: T,
  event: AgentTimingEvent,
): T {
  const exactKey = timingKey(event);
  const agentKind = event.agent_kind;
  if (exactKey === null || !agentKind) return state;
  const provenance = timingProvenance(state);
  const activeAgentStarts = {...(state.activeAgentStarts ?? {})};
  const activeKey = findActiveTimingKey(activeAgentStarts, event, exactKey);
  const startedAt = activeKey === null ? undefined : activeAgentStarts[activeKey];
  const activeStarts = {...provenance.activeStarts};
  const startProvenance = activeKey === null ? undefined : activeStarts[activeKey];
  if (activeKey !== null) {
    delete activeAgentStarts[activeKey];
    delete activeStarts[activeKey];
  }
  const next = {
    ...state,
    ...(startedAt === undefined
      ? {}
      : {
          agentIntervals: [
            ...(state.agentIntervals ?? []),
            {startedAt, finishedAt: event.timestamp},
          ],
        }),
    activeAgentStarts,
  };
  recordTimingProvenance(next, {
    activeStarts,
    intervals:
      startedAt === undefined
        ? provenance.intervals
        : [
            ...provenance.intervals,
            {
              startKey: activeKey as string,
              startSequence: startProvenance?.sequence ?? null,
              startCompatibility: startProvenance?.compatibility ?? false,
              finishKey: exactKey,
              agentKind,
              finishSequence: event.sequence ?? null,
              finishCompatibility: event.type === 'phase_finished',
            },
          ],
    unmatchedFinishes:
      startedAt === undefined
        ? [
            ...provenance.unmatchedFinishes,
            {
              key: exactKey,
              agentKind,
              finishedAt: event.timestamp,
              sequence: event.sequence ?? null,
              compatibility: event.type === 'phase_finished',
            },
          ]
        : provenance.unmatchedFinishes,
    closeouts: provenance.closeouts,
  });
  return next;
}

export function closeActiveAgentTimings<T extends RoundTimingState>(
  state: T,
  timestamp: string,
  sequence: number | null = null,
): T {
  const activeEntries = Object.entries(state.activeAgentStarts ?? {});
  const activeIntervals = activeEntries.map(([, startedAt]) => ({
    startedAt,
    finishedAt: timestamp,
  }));
  const provenance = timingProvenance(state);
  const next = {
    ...state,
    agentIntervals: [...(state.agentIntervals ?? []), ...activeIntervals],
    activeAgentStarts: {},
  };
  recordTimingProvenance(next, {
    activeStarts: {},
    intervals: [
      ...provenance.intervals,
      ...activeEntries.map(([key]) => ({
        startKey: key,
        startSequence: provenance.activeStarts[key]?.sequence ?? null,
        startCompatibility: provenance.activeStarts[key]?.compatibility ?? false,
        finishKey: key,
        agentKind: agentKindFromKey(key),
        finishSequence: sequence,
        finishCompatibility: false,
      })),
    ],
    unmatchedFinishes: provenance.unmatchedFinishes,
    closeouts: [...provenance.closeouts, {finishedAt: timestamp, sequence}],
  });
  return next;
}

/** Merges timing facts from an older prefix under a newer suffix. */
export function mergeAgentTimingPrefix(
  older: RoundTimingState,
  newer: RoundTimingState,
): RoundTimingState {
  const taggedIntervals: TaggedInterval[] = [];
  const operations: TimingOperation[] = [];
  collectTimingFacts(older, timingProvenance(older), taggedIntervals, operations);
  collectTimingFacts(newer, timingProvenance(newer), taggedIntervals, operations);
  operations.sort(compareTimingOperations);

  const active = new Map<string, TimingStart>();
  const completed = new Set<string>();
  const unmatchedFinishes: TimingFinish[] = [];
  for (const operation of operations) {
    if (operation.kind === 'start') {
      if (operation.start.compatibility && active.has(operation.start.key)) continue;
      active.set(operation.start.key, operation.start);
      completed.delete(operation.start.key);
      continue;
    }
    if (operation.kind === 'closeout') {
      for (const [startKey, start] of active) {
        taggedIntervals.push({
          interval: {startedAt: start.startedAt, finishedAt: operation.closeout.finishedAt},
          provenance: {
            startKey,
            startSequence: start.sequence,
            startCompatibility: start.compatibility,
            finishKey: startKey,
            agentKind: start.agentKind,
            finishSequence: operation.closeout.sequence,
            finishCompatibility: false,
          },
        });
      }
      active.clear();
      continue;
    }
    const finish = operation.finish;
    if (finish.compatibility && completed.has(finish.key)) continue;
    const startKey = findReplayTimingKey(active, finish);
    if (startKey === null) {
      unmatchedFinishes.push(finish);
      completed.add(finish.key);
      continue;
    }
    const start = active.get(startKey) as TimingStart;
    active.delete(startKey);
    taggedIntervals.push({
      interval: {startedAt: start.startedAt, finishedAt: finish.finishedAt},
      provenance: {
        startKey,
        startSequence: start.sequence,
        startCompatibility: start.compatibility,
        finishKey: finish.key,
        agentKind: finish.agentKind,
        finishSequence: finish.sequence,
        finishCompatibility: finish.compatibility,
      },
    });
    completed.add(finish.key);
  }

  taggedIntervals.sort(compareTaggedIntervals);
  const activeAgentStarts = Object.fromEntries(
    [...active].map(([key, start]) => [key, start.startedAt]),
  );
  const activeStarts = Object.fromEntries(
    [...active].map(([key, start]) => [
      key,
      {sequence: start.sequence, compatibility: start.compatibility},
    ]),
  );
  const hadIntervals = older.agentIntervals !== undefined || newer.agentIntervals !== undefined;
  const hadStarts = older.activeAgentStarts !== undefined || newer.activeAgentStarts !== undefined;
  const merged: RoundTimingState = {
    ...(hadIntervals || taggedIntervals.length > 0
      ? {agentIntervals: taggedIntervals.map(tagged => tagged.interval)}
      : {}),
    ...(hadStarts || active.size > 0 ? {activeAgentStarts} : {}),
  };
  recordTimingProvenance(merged, {
    activeStarts,
    intervals: taggedIntervals.map(tagged => tagged.provenance),
    unmatchedFinishes,
    closeouts: mergeCloseouts(timingProvenance(older).closeouts, timingProvenance(newer).closeouts),
  });
  return merged;
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

interface TaggedInterval {
  interval: AgentTimingInterval;
  provenance: TimingIntervalProvenance | null;
}

interface TimingStart {
  key: string;
  agentKind: string;
  startedAt: string;
  sequence: number | null;
  compatibility: boolean;
}

type TimingOperation =
  | {kind: 'start'; start: TimingStart}
  | {kind: 'finish'; finish: TimingFinish}
  | {kind: 'closeout'; closeout: TimingCloseout};

function timingProvenance(state: RoundTimingState): AgentTimingProvenance {
  const provenance =
    (state.activeAgentStarts === undefined
      ? undefined
      : timingProvenances.get(state.activeAgentStarts)) ??
    (state.agentIntervals === undefined ? undefined : timingProvenances.get(state.agentIntervals));
  return {
    activeStarts: provenance?.activeStarts ?? {},
    intervals: provenance?.intervals ?? (state.agentIntervals ?? []).map(() => null),
    unmatchedFinishes: provenance?.unmatchedFinishes ?? [],
    closeouts: provenance?.closeouts ?? [],
  };
}

function recordTimingProvenance(state: RoundTimingState, provenance: AgentTimingProvenance): void {
  if (state.activeAgentStarts !== undefined) {
    timingProvenances.set(state.activeAgentStarts, provenance);
  }
  if (state.agentIntervals !== undefined) timingProvenances.set(state.agentIntervals, provenance);
}

function collectTimingFacts(
  state: RoundTimingState,
  provenance: AgentTimingProvenance,
  fixed: TaggedInterval[],
  operations: TimingOperation[],
): void {
  for (const [index, interval] of (state.agentIntervals ?? []).entries()) {
    const endpoints = provenance.intervals[index] ?? null;
    if (endpoints === null) {
      fixed.push({interval, provenance: null});
      continue;
    }
    operations.push({
      kind: 'start',
      start: {
        key: endpoints.startKey,
        agentKind: agentKindFromKey(endpoints.startKey),
        startedAt: interval.startedAt,
        sequence: endpoints.startSequence,
        compatibility: endpoints.startCompatibility,
      },
    });
    operations.push({
      kind: 'finish',
      finish: {
        key: endpoints.finishKey,
        agentKind: endpoints.agentKind,
        finishedAt: interval.finishedAt,
        sequence: endpoints.finishSequence,
        compatibility: endpoints.finishCompatibility,
      },
    });
  }
  for (const [key, startedAt] of Object.entries(state.activeAgentStarts ?? {})) {
    operations.push({
      kind: 'start',
      start: {
        key,
        agentKind: agentKindFromKey(key),
        startedAt,
        sequence: provenance.activeStarts[key]?.sequence ?? null,
        compatibility: provenance.activeStarts[key]?.compatibility ?? false,
      },
    });
  }
  for (const finish of provenance.unmatchedFinishes) operations.push({kind: 'finish', finish});
  for (const closeout of provenance.closeouts) operations.push({kind: 'closeout', closeout});
}

function compareTimingOperations(left: TimingOperation, right: TimingOperation): number {
  const leftSequence = operationSequence(left);
  const rightSequence = operationSequence(right);
  if (leftSequence !== null && rightSequence !== null) return leftSequence - rightSequence;
  return operationTimestamp(left) - operationTimestamp(right);
}

function operationSequence(operation: TimingOperation): number | null {
  if (operation.kind === 'start') return operation.start.sequence;
  return operation.kind === 'finish' ? operation.finish.sequence : operation.closeout.sequence;
}

function operationTimestamp(operation: TimingOperation): number {
  const timestamp =
    operation.kind === 'start'
      ? operation.start.startedAt
      : operation.kind === 'finish'
        ? operation.finish.finishedAt
        : operation.closeout.finishedAt;
  return new Date(timestamp).getTime();
}

function mergeCloseouts(
  older: readonly TimingCloseout[],
  newer: readonly TimingCloseout[],
): TimingCloseout[] {
  return [...older, ...newer].sort((left, right) => {
    if (left.sequence !== null && right.sequence !== null) return left.sequence - right.sequence;
    return new Date(left.finishedAt).getTime() - new Date(right.finishedAt).getTime();
  });
}

function findReplayTimingKey(
  active: ReadonlyMap<string, TimingStart>,
  finish: TimingFinish,
): string | null {
  if (active.has(finish.key)) return finish.key;
  let first: TimingStart | null = null;
  for (const start of active.values()) {
    if (start.agentKind !== finish.agentKind) continue;
    if (
      first === null ||
      new Date(start.startedAt).getTime() < new Date(first.startedAt).getTime()
    ) {
      first = start;
    }
  }
  return first?.key ?? null;
}

function compareTaggedIntervals(left: TaggedInterval, right: TaggedInterval): number {
  const leftSequence = left.provenance?.finishSequence ?? null;
  const rightSequence = right.provenance?.finishSequence ?? null;
  if (leftSequence !== null && rightSequence !== null) return leftSequence - rightSequence;
  return (
    new Date(left.interval.finishedAt).getTime() - new Date(right.interval.finishedAt).getTime()
  );
}

function timingKey(event: AgentTimingEvent): string | null {
  if (!event.agent_kind) return null;
  return `${event.agent_kind}:${event.execution_id ?? event.invocation_id ?? ''}`;
}

function agentKindFromKey(key: string): string {
  const separator = key.indexOf(':');
  return separator === -1 ? key : key.slice(0, separator);
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

function intervalUnionElapsedMs(intervals: AgentTimingInterval[]): number {
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
