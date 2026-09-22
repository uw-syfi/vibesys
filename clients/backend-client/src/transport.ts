import type {Diagnostic, ProtocolResponse, ServerMessage} from './protocol.js';

export interface EventSubscription {
  close(): Promise<void>;
}

export interface SubscribeOptions {
  /**
   * Replay at most this many of the newest events instead of the whole history.
   * A server that predates the field forbids it and rejects the subscription,
   * which is exactly how a caller probes for the capability.
   */
  tail?: number;
}

/** A failed server response, including its optional structured diagnostic. */
export class ServerError extends Error {
  constructor(
    message: string,
    readonly diagnostic: Diagnostic | null = null,
  ) {
    super(message);
    this.name = 'ServerError';
  }
}

export function responseError(response: ProtocolResponse): ServerError {
  return new ServerError(response.error ?? 'Unknown server error', response.diagnostic ?? null);
}

export function parseProtocolResponse(line: string): ProtocolResponse {
  const value = parseRecord(line, 'response');
  if (value['protocol_version'] !== 1) throw new Error('Unsupported server protocol version');
  if (typeof value['request_id'] !== 'string') {
    throw new Error('Invalid server response: request_id must be a string');
  }
  if (typeof value['ok'] !== 'boolean') {
    throw new Error('Invalid server response: ok must be a boolean');
  }
  return value as unknown as ProtocolResponse;
}

export function parseServerMessage(line: string): ServerMessage {
  const value = parseRecord(line, 'event-stream message');
  const type = value['type'];
  if (type === 'subscribed') {
    if (
      typeof value['request_id'] !== 'string' ||
      typeof value['run_id'] !== 'string' ||
      typeof value['latest_sequence'] !== 'number'
    ) {
      throw new Error('Invalid subscribed message');
    }
  } else if (type === 'event') {
    if (!isRecord(value['event'])) throw new Error('Invalid event message');
  } else if (type === 'event_batch') {
    if (!Array.isArray(value['events'])) throw new Error('Invalid event batch message');
  } else if (type === 'protocol_error') {
    if (typeof value['code'] !== 'string' || typeof value['message'] !== 'string') {
      throw new Error('Invalid protocol error message');
    }
  } else {
    throw new Error(`Unknown server event-stream message: ${String(type)}`);
  }
  return value as unknown as ServerMessage;
}

function parseRecord(line: string, description: string): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch (error) {
    throw new Error(
      `Invalid server ${description} JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (!isRecord(value)) throw new Error(`Invalid server ${description}: expected an object`);
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
