import {create, fromJsonString, type JsonObject, toJsonString} from '@bufbuild/protobuf';
import {type Timestamp, timestampDate, timestampFromDate} from '@bufbuild/protobuf/wkt';
import {protocolErrorToServerError} from './errors.js';
import {
  PROTOCOL_VERSION,
  type ProtocolRequest,
  type ProtocolResponse,
  type RequestBody,
  RequestSchema,
  ResponseSchema,
  type RunEvent,
  RunEventSchema,
  type ServerMessage,
  ServerMessageSchema,
} from './protocol.js';

/** Server-side bound on the events long-poll wait. */
export const MAX_EVENTS_TIMEOUT_MS = 30_000;

/** A request the client refuses to send because it violates a server-enforced bound. */
export class RequestValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RequestValidationError';
  }
}

const JSON_OPTIONS = {useProtoFieldName: true} as const;

/**
 * Build a request envelope around `body`, filling protocol version, request id,
 * and timestamp, and rejecting values the server would reject.
 */
export function buildRequest(
  body: RequestBody,
  requestId: string,
  now: Date = new Date(),
): ProtocolRequest {
  const request = create(RequestSchema, {
    protocolVersion: PROTOCOL_VERSION,
    requestId,
    timestamp: timestampFromDate(now),
    body,
  });
  validateRequest(request);
  return request;
}

/** Mirror the server's request bounds so a bad value fails before the socket write. */
export function validateRequest(request: ProtocolRequest): void {
  const body = request.body;
  switch (body.case) {
    case 'steer':
      if (body.value.text.length === 0) throw new RequestValidationError('steer text is empty');
      break;
    case 'subscribe':
      if (body.value.tail !== undefined && body.value.tail < 1) {
        throw new RequestValidationError('subscribe tail must be at least 1');
      }
      break;
    case 'events':
      if (body.value.beforeSequence !== undefined && body.value.beforeSequence < 1) {
        throw new RequestValidationError('events beforeSequence must be at least 1');
      }
      if (body.value.timeoutMs > MAX_EVENTS_TIMEOUT_MS) {
        throw new RequestValidationError(
          `events timeoutMs must be at most ${MAX_EVENTS_TIMEOUT_MS}`,
        );
      }
      break;
    case undefined:
      throw new RequestValidationError('request must set exactly one body');
    default:
      break;
  }
}

/** Encode a request as one JSON line body (no trailing newline), with proto field names. */
export function encodeRequest(request: ProtocolRequest): string {
  return toJsonString(RequestSchema, request, JSON_OPTIONS);
}

/** Decode one event-stream line. Unknown fields are rejected. */
export function decodeServerMessage(line: string): ServerMessage {
  const message = fromJsonString(ServerMessageSchema, line);
  if (message.body.case === undefined) throw new Error('Invalid server message: no body set');
  return message;
}

/**
 * Decode one reply line. A `protocol_error` line (for example a rejected
 * protocol version) is thrown as a `ServerError`.
 */
export function decodeResponse(line: string): ProtocolResponse {
  const record = parseObject(line);
  if ('protocol_error' in record || 'protocolError' in record) {
    const message = decodeServerMessage(line);
    if (message.body.case === 'protocolError') {
      throw protocolErrorToServerError(message.body.value);
    }
  }
  const response = fromJsonString(ResponseSchema, line);
  if (response.protocolVersion !== PROTOCOL_VERSION) {
    throw new Error('Unsupported server protocol version');
  }
  return response;
}

/** Decode one run event from its JSON text. */
export function decodeRunEvent(line: string): RunEvent {
  return fromJsonString(RunEventSchema, line);
}

/** ISO-8601 (UTC, millisecond precision) form of a wire timestamp; an absent one reads as the epoch. */
export function timestampToIso(timestamp: Timestamp | undefined): string {
  return timestamp === undefined
    ? new Date(0).toISOString()
    : timestampDate(timestamp).toISOString();
}

function parseObject(line: string): JsonObject {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch (error) {
    throw new Error(
      `Invalid server response JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('Invalid server response: expected an object');
  }
  return value as JsonObject;
}
