import {createConnection, type Socket} from 'node:net';

/** Remove one listener; idempotent and never throws, including after close. */
export type RemoveSocketListener = () => void;

/**
 * The stream operations the Node transport needs from one client connection.
 *
 * Event registration returns its cleanup instead of exposing Node's
 * `EventEmitter`. That keeps the transport's ownership code independent of a
 * concrete socket while still letting each operation release exactly the
 * listeners it acquired.
 */
export interface ClientSocket {
  readonly closed: boolean;
  setTextMode(): void;
  send(text: string, onComplete?: (error?: Error | null) => void): void;
  end(): void;
  destroy(): void;
  listenConnect(listener: () => void): RemoveSocketListener;
  listenData(listener: (chunk: string) => void): RemoveSocketListener;
  listenError(listener: (error: Error) => void): RemoveSocketListener;
  listenPeerEnd(listener: () => void): RemoveSocketListener;
  listenClose(listener: () => void): RemoveSocketListener;
}

/** Opens client connections to the backend's local transport address. */
export interface ClientNetwork {
  connect(path: string): ClientSocket;
}

/** The production `ClientNetwork`, backed by Node Unix-domain sockets. */
export class UnixClientNetwork implements ClientNetwork {
  connect(path: string): ClientSocket {
    return new UnixClientSocket(createConnection(path));
  }
}

/** Adapt one Node socket to the small role consumed by `ServerClient`. */
class UnixClientSocket implements ClientSocket {
  readonly #socket: Socket;

  constructor(socket: Socket) {
    this.#socket = socket;
  }

  get closed(): boolean {
    // `destroyed` flips before Node emits the terminal `close` event. Owners
    // that join teardown need the event-complete state, not merely a requested
    // destroy, so they cannot return while the descriptor is still closing.
    return this.#socket.readyState === 'closed';
  }

  setTextMode(): void {
    this.#socket.setEncoding('utf8');
  }

  send(text: string, onComplete?: (error?: Error | null) => void): void {
    if (onComplete === undefined) {
      this.#socket.write(text);
      return;
    }
    this.#socket.write(text, onComplete);
  }

  end(): void {
    this.#socket.end();
  }

  destroy(): void {
    this.#socket.destroy();
  }

  listenConnect(listener: () => void): RemoveSocketListener {
    this.#socket.once('connect', listener);
    return () => this.#socket.off('connect', listener);
  }

  listenData(listener: (chunk: string) => void): RemoveSocketListener {
    this.#socket.on('data', listener);
    return () => this.#socket.off('data', listener);
  }

  listenError(listener: (error: Error) => void): RemoveSocketListener {
    this.#socket.on('error', listener);
    return () => this.#socket.off('error', listener);
  }

  listenPeerEnd(listener: () => void): RemoveSocketListener {
    // Listen for `end` as well as `close`: Bun can suppress `close` when a
    // write races a peer FIN. This protocol never half-closes normally, so
    // either event means no further response can arrive.
    let reported = false;
    const once = (): void => {
      if (reported) return;
      reported = true;
      listener();
    };
    this.#socket.once('end', once);
    this.#socket.once('close', once);
    return () => {
      this.#socket.off('end', once);
      this.#socket.off('close', once);
    };
  }

  listenClose(listener: () => void): RemoveSocketListener {
    this.#socket.once('close', listener);
    return () => this.#socket.off('close', listener);
  }
}
