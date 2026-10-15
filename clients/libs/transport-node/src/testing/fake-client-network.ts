import type {ClientNetwork, ClientSocket, RemoveSocketListener} from '../client-network.js';

type Listener<T extends readonly unknown[]> = (...values: T) => void;

/** The five listener roles a `ClientSocket` exposes to its owner. */
export type ClientSocketListenerKind = 'connect' | 'data' | 'error' | 'peerEnd' | 'close';

interface ListenerFailure {
  readonly error: Error;
  remainingAcquisitions: number;
}

type ConnectListenerPlan =
  | {readonly kind: 'manual'}
  | {readonly kind: 'immediate'}
  | {readonly kind: 'throw'; readonly error: Error};

/** A deterministic in-memory `ClientSocket` controlled only by test actions. */
export class FakeClientSocket implements ClientSocket {
  readonly path: string;
  readonly writes: string[] = [];
  readonly #connectListeners = new Set<Listener<[]>>();
  readonly #dataListeners = new Set<Listener<[string]>>();
  readonly #errorListeners = new Set<Listener<[Error]>>();
  readonly #peerEndListeners = new Set<Listener<[]>>();
  readonly #closeListeners = new Set<Listener<[]>>();
  readonly #listenerReleaseWaiters = new Set<() => void>();
  readonly #connectListenerPlan: ConnectListenerPlan;
  readonly #listenerFailures: Map<ClientSocketListenerKind, ListenerFailure>;
  #closed = false;
  #connected = false;
  #holdGracefulEnd = false;

  constructor(
    path: string,
    connectListenerPlan: ConnectListenerPlan = {kind: 'manual'},
    listenerFailures: Map<ClientSocketListenerKind, ListenerFailure> = new Map(),
  ) {
    this.path = path;
    this.#connectListenerPlan = connectListenerPlan;
    this.#listenerFailures = listenerFailures;
  }

  get closed(): boolean {
    return this.#closed;
  }

  /** Number of listeners still owned by this socket's consumers. */
  get activeListenerCount(): number {
    return (
      this.#connectListeners.size +
      this.#dataListeners.size +
      this.#errorListeners.size +
      this.#peerEndListeners.size +
      this.#closeListeners.size
    );
  }

  /** Resolve once this socket has released every acquired listener. */
  whenListenersReleased(): Promise<void> {
    if (this.activeListenerCount === 0) return Promise.resolve();
    return new Promise(resolve => this.#listenerReleaseWaiters.add(resolve));
  }

  /** Complete this socket's pending connection attempt. */
  connect(): void {
    if (this.#closed) throw new Error('cannot connect a closed FakeClientSocket');
    if (this.#connected) return;
    this.#connected = true;
    this.#emitOnce(this.#connectListeners);
  }

  /** Deliver one decoded text chunk from the fake peer. */
  receive(chunk: string): void {
    if (!this.#connected || this.#closed) throw new Error('FakeClientSocket is not connected');
    this.#emit(this.#dataListeners, chunk);
  }

  /** Deliver one transport failure without deciding whether the peer closes. */
  fail(error: Error): void {
    this.#emit(this.#errorListeners, error);
  }

  /** End the peer side and close the connection. */
  peerEnd(): void {
    this.#close();
  }

  /** Keep a graceful client end pending until the peer ends or the client destroys the socket. */
  holdGracefulEnd(): void {
    this.#holdGracefulEnd = true;
  }

  setTextMode(): void {}

  send(text: string, onComplete?: (error?: Error | null) => void): void {
    if (this.#closed) {
      onComplete?.(new Error('FakeClientSocket is closed'));
      return;
    }
    this.writes.push(text);
    onComplete?.();
  }

  end(): void {
    if (!this.#holdGracefulEnd) this.#close();
  }

  destroy(): void {
    this.#close();
  }

  listenConnect(listener: () => void): RemoveSocketListener {
    if (this.#connectListenerPlan.kind === 'throw') {
      throw this.#connectListenerPlan.error;
    }
    this.#raiseListenerFailure('connect');
    const remove = this.#listen(this.#connectListeners, listener);
    if (this.#connectListenerPlan.kind === 'immediate') this.connect();
    return remove;
  }

  listenData(listener: (chunk: string) => void): RemoveSocketListener {
    this.#raiseListenerFailure('data');
    return this.#listen(this.#dataListeners, listener);
  }

  listenError(listener: (error: Error) => void): RemoveSocketListener {
    this.#raiseListenerFailure('error');
    return this.#listen(this.#errorListeners, listener);
  }

  listenPeerEnd(listener: () => void): RemoveSocketListener {
    this.#raiseListenerFailure('peerEnd');
    return this.#listen(this.#peerEndListeners, listener);
  }

  listenClose(listener: () => void): RemoveSocketListener {
    this.#raiseListenerFailure('close');
    return this.#listen(this.#closeListeners, listener);
  }

  #close(): void {
    if (this.#closed) return;
    this.#closed = true;
    this.#emitOnce(this.#peerEndListeners);
    this.#emitOnce(this.#closeListeners);
  }

  #listen<T extends readonly unknown[]>(
    listeners: Set<Listener<T>>,
    listener: Listener<T>,
  ): RemoveSocketListener {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
      this.#notifyListenerRelease();
    };
  }

  #raiseListenerFailure(kind: ClientSocketListenerKind): void {
    const failure = this.#listenerFailures.get(kind);
    if (failure === undefined) return;
    failure.remainingAcquisitions -= 1;
    if (failure.remainingAcquisitions !== 0) return;
    this.#listenerFailures.delete(kind);
    throw failure.error;
  }

  #emit<T extends readonly unknown[]>(listeners: Set<Listener<T>>, ...values: T): void {
    for (const listener of [...listeners]) listener(...values);
  }

  #emitOnce<T extends readonly unknown[]>(listeners: Set<Listener<T>>, ...values: T): void {
    const pending = [...listeners];
    listeners.clear();
    for (const listener of pending) listener(...values);
    this.#notifyListenerRelease();
  }

  #notifyListenerRelease(): void {
    if (this.activeListenerCount !== 0) return;
    const waiters = [...this.#listenerReleaseWaiters];
    this.#listenerReleaseWaiters.clear();
    for (const resolve of waiters) resolve();
  }
}

/** The `ClientNetwork` Fake: every connection is explicit and inspectable. */
export class FakeClientNetwork implements ClientNetwork {
  readonly connections: FakeClientSocket[] = [];
  #nextConnectListenerPlan: ConnectListenerPlan = {kind: 'manual'};
  readonly #nextListenerFailures = new Map<ClientSocketListenerKind, ListenerFailure>();

  /** Make the next socket report connection while its connect listener is being acquired. */
  connectNextWhenObserved(): void {
    this.#nextConnectListenerPlan = {kind: 'immediate'};
  }

  /** Make acquisition of the next socket's connect listener fail structurally. */
  failNextConnectListener(error: Error): void {
    this.#nextConnectListenerPlan = {kind: 'throw', error};
  }

  /** Make one acquisition of a listener role on the next socket fail structurally. */
  failNextListener(kind: ClientSocketListenerKind, error: Error, acquisition = 1): void {
    if (!Number.isInteger(acquisition) || acquisition < 1) {
      throw new Error('listener acquisition must be a positive integer');
    }
    this.#nextListenerFailures.set(kind, {error, remainingAcquisitions: acquisition});
  }

  /** Make acquisition of the next socket's error listener fail structurally. */
  failNextErrorListener(error: Error): void {
    this.failNextListener('error', error);
  }

  connect(path: string): FakeClientSocket {
    const plan = this.#nextConnectListenerPlan;
    const listenerFailures = new Map(this.#nextListenerFailures);
    this.#nextConnectListenerPlan = {kind: 'manual'};
    this.#nextListenerFailures.clear();
    const socket = new FakeClientSocket(path, plan, listenerFailures);
    this.connections.push(socket);
    return socket;
  }

  get liveConnections(): readonly FakeClientSocket[] {
    return this.connections.filter(socket => !socket.closed);
  }
}
