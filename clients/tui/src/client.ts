import {randomUUID} from 'node:crypto';
import {createConnection, type Socket} from 'node:net';
import type {ProtocolRequest, ProtocolResponse, RequestInput, ServerMessage} from './protocol.js';

export interface EventSubscription {
  close(): Promise<void>;
}

export interface SupervisionClientOptions {
  connectTimeoutMs?: number;
  requestTimeoutMs?: number;
}

const DEFAULT_CONNECT_TIMEOUT_MS = 5_000;
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;

export class SupervisionClient {
  readonly #socket: Socket;
  readonly #path: string;
  readonly #pending = new Map<
    string,
    {
      resolve: (value: ProtocolResponse) => void;
      reject: (error: Error) => void;
      timeout: ReturnType<typeof setTimeout>;
    }
  >();
  readonly #connectTimeoutMs: number;
  readonly #requestTimeoutMs: number;
  #buffer = '';

  private constructor(socket: Socket, path: string, options: SupervisionClientOptions) {
    this.#socket = socket;
    this.#path = path;
    this.#connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    socket.setEncoding('utf8');
    socket.on('data', chunk => this.#onData(chunk.toString()));
    socket.on('error', error => this.#rejectAll(error));
    socket.on('close', () => this.#rejectAll(new Error('Supervision server disconnected')));
  }

  static connect(path: string, options: SupervisionClientOptions = {}): Promise<SupervisionClient> {
    return new Promise((resolve, reject) => {
      const socket = createConnection(path);
      const onError = (error: Error): void => {
        clearTimeout(timeout);
        reject(error);
      };
      const timeout = setTimeout(() => {
        socket.destroy();
        reject(
          new Error(
            `Timed out connecting to supervision server after ${options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS}ms`,
          ),
        );
      }, options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS);
      socket.once('connect', () => {
        clearTimeout(timeout);
        socket.off('error', onError);
        resolve(new SupervisionClient(socket, path, options));
      });
      socket.once('error', onError);
    });
  }

  request(input: RequestInput): Promise<ProtocolResponse> {
    if (this.#socket.destroyed) {
      return Promise.reject(new Error('Supervision server is disconnected'));
    }
    const requestId = randomUUID();
    const request = {
      protocol_version: 1,
      request_id: requestId,
      timestamp: new Date().toISOString(),
      ...input,
    } as ProtocolRequest;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.#pending.delete(requestId);
        reject(new Error(`Supervision request timed out after ${this.#requestTimeoutMs}ms`));
      }, this.#requestTimeoutMs);
      this.#pending.set(requestId, {resolve, reject, timeout});
      this.#socket.write(`${JSON.stringify(request)}\n`, error => {
        if (error) this.#rejectPending(requestId, error);
      });
    });
  }

  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    return new Promise((resolve, reject) => {
      const socket = createConnection(this.#path);
      let buffer = '';
      let subscribed = false;
      let closing = false;
      let disconnected = false;
      const handshakeTimeout = setTimeout(() => {
        disconnect(
          new Error(`Supervision subscription timed out after ${this.#connectTimeoutMs}ms`),
        );
        socket.destroy();
      }, this.#connectTimeoutMs);
      const disconnect = (error: Error): void => {
        if (disconnected || closing) return;
        disconnected = true;
        clearTimeout(handshakeTimeout);
        if (subscribed) onDisconnect(error);
        else reject(error);
      };
      socket.setEncoding('utf8');
      socket.once('connect', () => {
        socket.write(
          `${JSON.stringify({
            protocol_version: 1,
            request_id: randomUUID(),
            timestamp: new Date().toISOString(),
            type: 'subscribe',
            after_sequence: afterSequence,
          })}\n`,
        );
      });
      socket.on('data', chunk => {
        buffer += chunk.toString();
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          if (!line) continue;
          let message: ServerMessage;
          try {
            message = parseServerMessage(line);
          } catch (error) {
            const parseError = error instanceof Error ? error : new Error(String(error));
            disconnect(parseError);
            socket.destroy();
            return;
          }
          try {
            onMessage(message);
          } catch (error) {
            disconnect(error instanceof Error ? error : new Error(String(error)));
            socket.destroy();
            return;
          }
          if (!subscribed && message.type === 'subscribed') {
            subscribed = true;
            clearTimeout(handshakeTimeout);
            resolve({
              close: () => {
                closing = true;
                clearTimeout(handshakeTimeout);
                return closeSocket(socket);
              },
            });
          }
        }
      });
      socket.once('error', disconnect);
      socket.once('close', () => {
        disconnect(
          new Error(
            subscribed
              ? 'Supervision event stream disconnected'
              : 'Supervision event stream disconnected before subscription',
          ),
        );
      });
    });
  }

  close(): Promise<void> {
    return new Promise(resolve => {
      if (this.#socket.destroyed) return resolve();
      this.#socket.once('close', resolve);
      this.#socket.end();
    });
  }

  #onData(chunk: string): void {
    this.#buffer += chunk;
    const lines = this.#buffer.split('\n');
    this.#buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line) continue;
      let response: ProtocolResponse;
      try {
        response = parseProtocolResponse(line);
      } catch (error) {
        const parseError = error instanceof Error ? error : new Error(String(error));
        this.#rejectAll(parseError);
        this.#socket.destroy();
        return;
      }
      const pending = this.#pending.get(response.request_id);
      if (!pending) continue;
      this.#pending.delete(response.request_id);
      clearTimeout(pending.timeout);
      if (response.ok) pending.resolve(response);
      else pending.reject(new Error(response.error ?? 'Unknown supervision error'));
    }
  }

  #rejectAll(error: Error): void {
    for (const pending of this.#pending.values()) {
      clearTimeout(pending.timeout);
      pending.reject(error);
    }
    this.#pending.clear();
  }

  #rejectPending(requestId: string, error: Error): void {
    const pending = this.#pending.get(requestId);
    if (pending === undefined) return;
    this.#pending.delete(requestId);
    clearTimeout(pending.timeout);
    pending.reject(error);
  }
}

function closeSocket(socket: Socket): Promise<void> {
  return new Promise(resolve => {
    if (socket.destroyed) return resolve();
    socket.once('close', resolve);
    socket.end();
  });
}

function parseProtocolResponse(line: string): ProtocolResponse {
  const value = parseRecord(line, 'response');
  if (value['protocol_version'] !== 1) throw new Error('Unsupported supervision protocol version');
  if (typeof value['request_id'] !== 'string') {
    throw new Error('Invalid supervision response: request_id must be a string');
  }
  if (typeof value['ok'] !== 'boolean') {
    throw new Error('Invalid supervision response: ok must be a boolean');
  }
  return value as unknown as ProtocolResponse;
}

function parseServerMessage(line: string): ServerMessage {
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
    throw new Error(`Unknown supervision event-stream message: ${String(type)}`);
  }
  return value as unknown as ServerMessage;
}

function parseRecord(line: string, description: string): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch (error) {
    throw new Error(
      `Invalid supervision ${description} JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (!isRecord(value)) throw new Error(`Invalid supervision ${description}: expected an object`);
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
