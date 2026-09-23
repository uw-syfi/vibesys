import {BackendClientError, ServerError} from './errors.js';
import type {Diagnostic, ProtocolResponse, ServerMessage} from './protocol.js';

/** Parse and validate a response independently of the transport framing. */
export function parseProtocolResponse(line: string): ProtocolResponse {
  const value = parseRecord(line, 'response');
  if (value['protocol_version'] !== 1) {
    throw new BackendClientError('parse', 'Unsupported server protocol version');
  }
  if (typeof value['request_id'] !== 'string') {
    throw new BackendClientError('parse', 'Invalid server response: request_id must be a string');
  }
  if (typeof value['ok'] !== 'boolean') {
    throw new BackendClientError('parse', 'Invalid server response: ok must be a boolean');
  }
  return value as unknown as ProtocolResponse;
}

/** Parse and validate a streamed server message independently of framing. */
export function parseServerMessage(line: string): ServerMessage {
  const value = parseRecord(line, 'event-stream message');
  const type = value['type'];
  if (type === 'subscribed') {
    if (
      typeof value['request_id'] !== 'string' ||
      typeof value['run_id'] !== 'string' ||
      typeof value['latest_sequence'] !== 'number'
    ) {
      throw new BackendClientError('parse', 'Invalid subscribed message');
    }
  } else if (type === 'event') {
    if (!isRecord(value['event'])) throw new BackendClientError('parse', 'Invalid event message');
  } else if (type === 'event_batch') {
    if (!Array.isArray(value['events'])) {
      throw new BackendClientError('parse', 'Invalid event batch message');
    }
  } else if (type === 'protocol_error') {
    if (typeof value['code'] !== 'string' || typeof value['message'] !== 'string') {
      throw new BackendClientError('parse', 'Invalid protocol error message');
    }
  } else {
    throw unknownStreamLineError(value);
  }
  return value as unknown as ServerMessage;
}

/** Convert an invalid frame or callback exception into the public error taxonomy. */
export function streamFailure(error: unknown): BackendClientError {
  if (error instanceof BackendClientError) return error;
  const cause = toError(error);
  return new BackendClientError('parse', cause.message, {cause});
}

export function responseError(response: ProtocolResponse): ServerError {
  return new ServerError(response.error ?? 'Unknown server error', response.diagnostic ?? null);
}

/**
 * A server that predates an optional subscribe field rejects it with a Response
 * frame on the stream connection. Treat that as a capability refusal, not a
 * malformed stream, so callers can probe and fall back.
 */
function unknownStreamLineError(value: Record<string, unknown>): BackendClientError {
  const rejected =
    value['type'] === undefined && value['ok'] === false && typeof value['request_id'] === 'string';
  if (!rejected) {
    return new BackendClientError(
      'parse',
      `Unknown server event-stream message: ${String(value['type'])}`,
    );
  }
  return new ServerError(
    typeof value['error'] === 'string' ? value['error'] : 'Server rejected the subscription',
    isRecord(value['diagnostic']) ? (value['diagnostic'] as unknown as Diagnostic) : null,
  );
}

function parseRecord(line: string, description: string): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch (error) {
    throw new BackendClientError(
      'parse',
      `Invalid server ${description} JSON: ${error instanceof Error ? error.message : String(error)}`,
      {cause: error},
    );
  }
  if (!isRecord(value)) {
    throw new BackendClientError('parse', `Invalid server ${description}: expected an object`);
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}
