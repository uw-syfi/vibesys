/**
 * An in-memory socket namespace: servers listen on paths, clients connect to them, and each
 * connection is a pair of linked in-memory byte streams. It is what the Fakes of the `Host` role
 * and of `LocalHost`'s system seam are made of, so both behave like the same network.
 *
 * The streams behave like a connected Unix socket as far as the relay can observe: bytes arrive in
 * order, ending one side ends the other's readable side, and destroying one side ends the other.
 */
import {Duplex} from 'node:stream';
import {HostError} from '../host.js';

/** A server's per-connection behavior: it receives the server end of each new connection. */
export type ConnectionHandler = (connection: Duplex) => void;

class LinkedEnd extends Duplex {
  peer: LinkedEnd | null = null;

  constructor() {
    super({allowHalfOpen: false});
  }

  override _read(): void {}

  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: () => void): void {
    this.peer?.push(chunk);
    callback();
  }

  override _final(callback: () => void): void {
    this.peer?.push(null);
    callback();
  }

  override _destroy(error: Error | null, callback: (error: Error | null) => void): void {
    const peer = this.peer;
    this.peer = null;
    if (peer !== null && !peer.readableEnded) peer.push(null);
    callback(error);
  }
}

/** Two linked stream ends: what one writes, the other reads. */
function linkedPair(): [Duplex, Duplex] {
  const left = new LinkedEnd();
  const right = new LinkedEnd();
  left.peer = right;
  right.peer = left;
  return [left, right];
}

export class FakeNetwork {
  readonly #listeners = new Map<string, ConnectionHandler>();
  readonly #connections = new Map<string, Set<Duplex>>();

  /** Accept connections at `path` until the returned function is called. */
  listen(path: string, handler: ConnectionHandler): () => void {
    if (this.#listeners.has(path)) throw new Error(`fake network: ${path} is already listening`);
    this.#listeners.set(path, handler);
    return () => this.unlisten(path);
  }

  /** Stop listening at `path` and drop every connection it accepted, like a server exiting. */
  unlisten(path: string): void {
    this.#listeners.delete(path);
    for (const connection of this.#connections.get(path) ?? []) connection.destroy();
    this.#connections.delete(path);
  }

  isListening(path: string): boolean {
    return this.#listeners.has(path);
  }

  /** Connect to `path`; rejects with `unreachable` when nothing listens there. */
  async connect(path: string): Promise<Duplex> {
    const handler = this.#listeners.get(path);
    if (handler === undefined) {
      throw new HostError('unreachable', `nothing is listening at ${path}`);
    }
    const [client, server] = linkedPair();
    let open = this.#connections.get(path);
    if (open === undefined) {
      open = new Set();
      this.#connections.set(path, open);
    }
    const connections = open;
    connections.add(server);
    server.once('close', () => connections.delete(server));
    handler(server);
    return client;
  }
}
