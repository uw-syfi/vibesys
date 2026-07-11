import {randomUUID} from 'node:crypto';
import {createConnection, type Socket} from 'node:net';
import type {ProtocolRequest, ProtocolResponse, RequestInput} from './protocol.js';

export class SupervisionClient {
  readonly #socket: Socket;
  readonly #pending = new Map<string, {resolve: (value: ProtocolResponse) => void; reject: (error: Error) => void}>();
  #buffer = '';

  private constructor(socket: Socket) {
    this.#socket = socket;
    socket.setEncoding('utf8');
    socket.on('data', chunk => this.#onData(chunk.toString()));
    socket.on('error', error => this.#rejectAll(error));
    socket.on('close', () => this.#rejectAll(new Error('Supervision server disconnected')));
  }

  static connect(path: string): Promise<SupervisionClient> {
    return new Promise((resolve, reject) => {
      const socket = createConnection(path);
      socket.once('connect', () => resolve(new SupervisionClient(socket)));
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
