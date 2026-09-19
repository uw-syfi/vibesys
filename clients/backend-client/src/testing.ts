/**
 * Concise builders for tests in this package and its consumers. Not part of the
 * runtime API: import from `@vibesys/backend-client/testing`.
 */
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {type Timestamp, timestampFromDate} from '@bufbuild/protobuf/wkt';
import {
  type EventType,
  PROTOCOL_VERSION,
  type RunEvent,
  RunEventSchema,
  type RunSnapshot,
  RunSnapshotSchema,
  RunStatus,
  type ServerMessage,
  ServerMessageSchema,
} from './protocol.js';

/** A `Timestamp` for an ISO-8601 string or a `Date`. */
export function timestampOf(value: string | Date): Timestamp {
  return timestampFromDate(typeof value === 'string' ? new Date(value) : value);
}

/**
 * A `RunEvent` of `type` with test defaults (sequence 1, run id `run`, a fixed
 * timestamp). `init` overrides any field, including the `data` oneof, for
 * example `makeEvent(EventType.CHAT, {data: {case: 'chat', value: {answer: 'hi'}}})`.
 */
export function makeEvent(
  type: EventType,
  init: MessageInitShape<typeof RunEventSchema> = {},
): RunEvent {
  return create(RunEventSchema, {
    protocolVersion: PROTOCOL_VERSION,
    sequence: 1,
    runId: 'run',
    timestamp: timestampOf('2026-01-01T00:00:00Z'),
    ...init,
    type,
  });
}

/** A `RunSnapshot` with test defaults (run id `run`, running, sequence 1). */
export function makeSnapshot(init: MessageInitShape<typeof RunSnapshotSchema> = {}): RunSnapshot {
  return create(RunSnapshotSchema, {
    protocolVersion: PROTOCOL_VERSION,
    runId: 'run',
    sequence: 1,
    status: RunStatus.RUNNING,
    ...init,
  });
}

/** A `ServerMessage` carrying an event batch of `events`. */
export function makeEventBatch(
  events: readonly RunEvent[],
  throughSequence: number = events.at(-1)?.sequence ?? 0,
  extra: {storeId?: string; historyAfterSequence?: number} = {},
): ServerMessage {
  return create(ServerMessageSchema, {
    body: {case: 'eventBatch', value: {events: [...events], throughSequence, ...extra}},
  });
}
