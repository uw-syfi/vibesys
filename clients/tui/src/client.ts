import {randomUUID} from 'node:crypto';
import {createConnection, type Socket} from 'node:net';
import type {
  ProtocolRequest,
  ProtocolResponse,
  RequestInput,
  ServerMessage,
} from './protocol.js';

export interface EventSubscription {
  close(): Promise<void>;
}

export class SupervisionClient {
  readonly #socket: Socket;
  readonly #path: string;
  readonly #pending = new Map<string, {resolve: (value: ProtocolResponse) => void; reject: (error: Error) => void}>();
  #buffer = '';

  private constructor(socket: Socket, path: string) {
    this.#socket = socket;
    this.#path = path;
    socket.setEncoding('utf8');
    socket.on('data', chunk => this.#onData(chunk.toString()));
    socket.on('error', error => this.#rejectAll(error));
    socket.on('close', () => this.#rejectAll(new Error('Supervision server disconnected')));
  }

  static connect(path: string): Promise<SupervisionClient> {
    return new Promise((resolve, reject) => {
      const socket = createConnection(path);
      socket.once('connect', () => resolve(new SupervisionClient(socket, path)));
      socket.once('error', reject);
    });
  }

  request(input: RequestInput): Promise<ProtocolResponse> {
    const requestId = randomUUID();
    const request = {
      protocol_version: 1,
      request_id: requestId,
      timestamp: new Date().toISOString(),
      ...input,
    } as ProtocolRequest;
    return new Promise((resolve, reject) => {
      this.#pending.set(requestId, {resolve, reject});
      this.#socket.write(`${JSON.stringify(request)}\n`);
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
      socket.setEncoding('utf8');
      socket.once('connect', () => {
        socket.write(`${JSON.stringify({
          protocol_version: 1,
          request_id: randomUUID(),
          timestamp: new Date().toISOString(),
          type: 'subscribe',
          after_sequence: afterSequence,
        })}\n`);
      });
      socket.on('data', chunk => {
        buffer += chunk.toString();
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          if (!line) continue;
          let message: ServerMessage;
          try {
            message = JSON.parse(line) as ServerMessage;
          } catch (error) {
            const parseError = error instanceof Error ? error : new Error(String(error));
            if (subscribed) onDisconnect(parseError);
            else reject(parseError);
            socket.destroy();
            return;
          }
          onMessage(message);
          if (!subscribed && message.type === 'subscribed') {
            subscribed = true;
            resolve({
              close: () => {
                closing = true;
                return closeSocket(socket);
              },
            });
          }
        }
      });
      socket.once('error', error => subscribed ? onDisconnect(error) : reject(error));
      socket.once('close', () => {
        if (subscribed && !closing) onDisconnect(new Error('Supervision event stream disconnected'));
        else reject(new Error('Supervision event stream disconnected before subscription'));
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
      const response = JSON.parse(line) as ProtocolResponse;
      const pending = this.#pending.get(response.request_id);
      if (!pending) continue;
      this.#pending.delete(response.request_id);
      if (response.ok) pending.resolve(response);
      else pending.reject(new Error(response.error ?? 'Unknown supervision error'));
    }
  }

  #rejectAll(error: Error): void {
    for (const pending of this.#pending.values()) pending.reject(error);
    this.#pending.clear();
  }
}

function closeSocket(socket: Socket): Promise<void> {
  return new Promise(resolve => {
    if (socket.destroyed) return resolve();
    socket.once('close', resolve);
    socket.end();
  });
}
