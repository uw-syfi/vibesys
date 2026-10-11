import type {
  AgentStatusData,
  EventBatchMessage,
  ProtocolResponse,
  RunEvent,
  RunSnapshot,
} from '../protocol.js';

const FIXTURE_EPOCH_MS = Date.UTC(2026, 0, 1);

type EventOverrides = Omit<Partial<RunEvent>, 'sequence' | 'type'>;
type EventEnvelopeOverrides = Omit<EventOverrides, 'data'>;
type RoundFinishedData = Extract<NonNullable<RunEvent['data']>, {kind: 'round_finished'}>;
type EventBatchOverrides = Omit<Partial<EventBatchMessage>, 'events' | 'type'>;

/** A deterministic UTC timestamp, `sequence` seconds after the fixture epoch. */
export function timestamp(sequence = 0): string {
  if (!Number.isSafeInteger(sequence) || sequence < 0) {
    throw new RangeError('Fixture sequence must be a non-negative safe integer');
  }
  const instant = new Date(FIXTURE_EPOCH_MS + sequence * 1_000);
  if (Number.isNaN(instant.valueOf()))
    throw new RangeError('Fixture sequence is outside Date range');
  return instant.toISOString().replace('.000Z', 'Z');
}

/** Build the smallest valid event envelope, with no invented run or agent context. */
export function event(
  sequence: number | undefined,
  type: RunEvent['type'],
  content: string,
): RunEvent;
export function event(
  sequence: number | undefined,
  type: RunEvent['type'],
  overrides?: EventOverrides,
): RunEvent;
export function event(
  sequence: number | undefined,
  type: RunEvent['type'],
  contentOrOverrides: string | EventOverrides = {},
): RunEvent {
  if (typeof contentOrOverrides === 'string' && type !== 'agent_output_chunk') {
    throw new TypeError('Event content shorthand requires type agent_output_chunk');
  }
  const overrides: EventOverrides = structuredClone(
    typeof contentOrOverrides === 'string'
      ? {
          data: {
            kind: 'agent_output_chunk',
            channel: 'assistant',
            content: contentOrOverrides,
          },
        }
      : contentOrOverrides,
  );
  const {data, ...envelope} = overrides;
  const built: RunEvent = {
    timestamp: timestamp(sequence),
    type,
    ...envelope,
    ...(sequence === undefined ? {} : {sequence}),
  };
  return data === undefined ? built : {...built, data};
}

/**
 * Build a completed first-round summary without fabricating benchmark evidence.
 * Tests about measured performance opt in through `dataOverrides`.
 */
export function roundFinishedEvent(
  sequence: number,
  dataOverrides: Partial<Omit<RoundFinishedData, 'kind'>> = {},
  eventOverrides: EventEnvelopeOverrides = {},
): RunEvent {
  return event(sequence, 'round_finished', {
    status: 'completed',
    round_label: 'round-1',
    ...eventOverrides,
    data: {
      kind: 'round_finished',
      attempts: 1,
      judge_verdict: 'pass',
      perf_metric: null,
      perf_unit: null,
      profile_skipped: false,
      ...dataOverrides,
    },
  });
}

/** Build one implementer status-bearing chunk or tool call. */
export function statusEvent(
  sequence: number,
  executionId: string,
  kind: 'agent_output_chunk' | 'tool_call',
  status: AgentStatusData = {progress: `step ${sequence}`},
  eventOverrides: EventEnvelopeOverrides = {},
): RunEvent {
  return event(sequence, kind, {
    agent_kind: 'implementer',
    round_label: 'round-1-implementer',
    execution_id: executionId,
    invocation_id: executionId,
    ...eventOverrides,
    data:
      kind === 'agent_output_chunk'
        ? {kind, channel: 'analysis', content: '', status: {...status}}
        : {kind, tool: 'Bash', call_id: `call-${sequence}`, args: {}, status: {...status}},
  });
}

/** Build an event in experiment-chat scope without inventing thread or invocation identity. */
export function chatEvent(
  sequence: number,
  type: RunEvent['type'],
  data: NonNullable<RunEvent['data']>,
  eventOverrides: EventEnvelopeOverrides = {},
): RunEvent {
  return event(sequence, type, {
    agent_kind: 'chat',
    round_label: 'experiment-chat',
    ...eventOverrides,
    data: {...data},
  });
}

/** Build an event batch, copying caller-owned arrays and omitting unspecified cursor metadata. */
export function eventBatch(
  events: readonly RunEvent[] = [],
  overrides: EventBatchOverrides = {},
): EventBatchMessage {
  const ownedOverrides = structuredClone(overrides);
  return {
    type: 'event_batch',
    ...ownedOverrides,
    events: structuredClone([...events]),
  };
}

/** Build a successful running snapshot response at sequence zero for `run-1`. */
export function snapshotResponse(overrides: Partial<RunSnapshot> = {}): ProtocolResponse {
  const ownedOverrides = structuredClone(overrides);
  return {
    ok: true,
    request_id: 'request-1',
    snapshot: {
      run_id: 'run-1',
      sequence: 0,
      status: 'running',
      ...ownedOverrides,
    },
  };
}
