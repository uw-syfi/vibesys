import type {ProtocolResponse, RequestInput, ServerMessage} from '@vibesys/contracts';
import {
  BackendClientError,
  parseProtocolResponse,
  parseServerMessage,
  responseError,
  ServerError,
  streamFailure,
} from '@vibesys/contracts';
import {
  type AbortRegistration,
  type AbortSignalLike,
  abortReason,
  ControlChannel,
  type ControlChannelState,
  type ControlConnection,
  type ControlConnectionHandlers,
  defaultScheduleTimeout,
  type EventSubscription,
  type IssuedRequest,
  NewlineFramer,
  type RequestOptions,
  registerAbort,
  type ScheduleTimeout,
  type SubscribeOptions,
  settleAll,
  subscribeRequest,
} from '@vibesys/transport';
import {
  type ClientNetwork,
  type ClientSocket,
  type RemoveSocketListener,
  UnixClientNetwork,
} from './client-network.js';

export interface ServerClientOptions {
  /** Stable frontend identity reflected by server acknowledgements. */
  clientId?: string;
  connectTimeoutMs?: number;
  requestTimeoutMs?: number;
  /** Delay between connection attempts while the socket does not exist yet. */
  connectRetryIntervalMs?: number;
  /**
   * How long `close()` waits for a socket to end gracefully before destroying
   * it. Bounds shutdown against a server that never sends its FIN, so a quit
   * cannot hang. Applies to every socket the client owns: control, chat, and
   * subscriptions.
   */
  closeGraceMs?: number;
  /**
   * Backoff schedule for control-channel redials after a drop; its length
   * bounds the attempts per outage. Defaults to the schedule the subscription
   * shares; injected by tests so a retry is immediate instead of half a second
   * away.
   */
  reconnectDelaysMs?: readonly number[];
  /**
   * Observe control-channel connectivity: `disconnected` once when a live
   * channel drops, `connected` when a redial recovers it. Not called on the
   * first successful connect (the channel is connected by default). Lets a
   * frontend disable the controls a dropped channel cannot carry. A thrown
   * observer error never interrupts connection ownership; `close()` reports it
   * after every acquired bounded close owner settles. If acquiring that bound
   * fails, the client starts a best-effort physical close before exposing the
   * structural error.
   */
  onConnectionState?: (state: ControlChannelState) => void;
  /** Clock and scheduler seam used by every transport deadline and retry. */
  clock?: ClientClock;
  /** Network implementation that owns creation of every client socket. */
  network?: ClientNetwork;
}

/**
 * The time source used by the Node transport.
 *
 * Keeping the reading and scheduling sides together makes a connect deadline
 * coherent: a Fake can advance both with one causal operation, while
 * production uses the system clock and timer queue.
 */
export interface ClientClock {
  now(): number;
  readonly scheduleTimeout: ScheduleTimeout;
}

const SYSTEM_CLOCK: ClientClock = {
  now: () => Date.now(),
  scheduleTimeout: defaultScheduleTimeout,
};
const UNIX_NETWORK = new UnixClientNetwork();

const DEFAULT_CONNECT_TIMEOUT_MS = 5_000;
const DEFAULT_CONNECT_RETRY_INTERVAL_MS = 25;
/**
 * A graceful end on a local socket completes in well under this; the window is
 * only reached when the peer never sends its FIN, so it stays short to keep
 * `close()` inside the launcher's 2s backend exit grace.
 */
const DEFAULT_CLOSE_GRACE_MS = 250;
/** Errors a not-yet-listening server produces; anything else is fatal. */
const RETRYABLE_CONNECT_CODES = new Set(['ENOENT', 'ECONNREFUSED']);

/**
 * The Node unix-socket transport: one control channel, plus a socket per
 * subscription and per dedicated request.
 *
 * Everything that decides what happens to a control request (policy, redial,
 * in-flight disposition, cancellation, connectivity reporting) lives in the
 * runtime-neutral `ControlChannel`; this class supplies the sockets and the
 * newline framing it runs over, so the browser transport answers those
 * questions the same way.
 */
export class ServerClient {
  readonly #path: string;
  readonly #clientId: string;
  readonly #connectTimeoutMs: number;
  readonly #closeGraceMs: number;
  readonly #clock: ClientClock;
  readonly #network: ClientNetwork;
  /**
   * Every secondary socket the client has opened (subscriptions and dedicated
   * requests), mapped to a hook that settles its own operation. `close()` calls
   * the hook before destroying, so a pending operation fails while tearing a
   * live subscription down does not surface as a spurious stream disconnect,
   * whatever order the caller closes the stream and the client in.
   */
  readonly #secondarySockets = new Map<ClientSocket, () => void>();
  /**
   * Graceful teardowns that have released their operation ownership but have
   * not reached the socket's terminal close event. Keeping them here lets a
   * client-wide close join the same teardown instead of losing the socket or
   * starting a second FIN/grace sequence.
   */
  readonly #retiringSockets = new Map<ClientSocket, Promise<void>>();
  /** Control dials created by a reconnect but not yet handed to the channel. */
  readonly #openingControlDials = new Map<ClientSocket, SocketDial>();
  readonly #channel: ControlChannel;

  #closed = false;
  #closing: Promise<void> | null = null;

  private constructor(path: string, options: ServerClientOptions) {
    this.#path = path;
    this.#clientId = options.clientId ?? globalThis.crypto.randomUUID();
    this.#connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.#closeGraceMs = options.closeGraceMs ?? DEFAULT_CLOSE_GRACE_MS;
    this.#clock = options.clock ?? SYSTEM_CLOCK;
    this.#network = options.network ?? UNIX_NETWORK;
    this.#channel = new ControlChannel(
      {
        open: handlers => this.#dialControl(handlers),
        runDedicated: (request, signal) => this.#requestLongRunning(request, signal),
      },
      {
        clientId: this.#clientId,
        requestTimeoutMs: options.requestTimeoutMs,
        reconnectDelaysMs: options.reconnectDelaysMs,
        onConnectionState: options.onConnectionState,
        scheduleTimeout: this.#clock.scheduleTimeout,
      },
    );
  }

  /**
   * Connect to the server socket, retrying until it accepts.
   *
   * The launcher starts the backend and this client concurrently, so the
   * socket routinely does not exist for the first few hundred milliseconds.
   * Only the errors a starting server produces are retried; every other
   * failure, and the overall deadline, still surfaces to the caller.
   */
  static async connect(path: string, options: ServerClientOptions = {}): Promise<ServerClient> {
    const timeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    const retryIntervalMs = options.connectRetryIntervalMs ?? DEFAULT_CONNECT_RETRY_INTERVAL_MS;
    const clock = options.clock ?? SYSTEM_CLOCK;
    const deadline = clock.now() + timeoutMs;
    let lastError: Error | undefined;
    while (true) {
      try {
        return await ServerClient.#connectOnce(path, options, deadline, clock);
      } catch (error) {
        if (!isTransientDialError(error)) throw error;
        lastError = error;
      }
      if (clock.now() + retryIntervalMs >= deadline) {
        throw new BackendClientError(
          'timeout',
          `Timed out connecting to server after ${timeoutMs}ms: ${lastError?.message}`,
          {cause: lastError},
        );
      }
      await delay(retryIntervalMs, clock.scheduleTimeout);
    }
  }

  static #connectOnce(
    path: string,
    options: ServerClientOptions,
    deadline: number,
    clock: ClientClock,
  ): Promise<ServerClient> {
    const configured = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    const dial = createSocketDial(
      path,
      Math.max(0, deadline - clock.now()),
      `Timed out connecting to server after ${configured}ms`,
      clock.scheduleTimeout,
      options.network ?? UNIX_NETWORK,
    );
    dial.start();
    return dial.connected.then(async socket => {
      const client = new ServerClient(path, options);
      await client.#adoptInitialControl(socket);
      return client;
    });
  }

  /** Transfer the already-connected initial socket into the control channel. */
  async #adoptInitialControl(socket: ClientSocket): Promise<void> {
    try {
      this.#channel.adopt(handlers => this.#controlConnection(socket, handlers));
    } catch (error) {
      // Listener setup failed before the channel could take ownership. Destroy
      // and join the socket, but preserve the acquisition failure as the
      // connection outcome even if close observation also fails.
      await destroySocket(socket).catch(() => undefined);
      throw error;
    }
  }

  /**
   * Send a control request and resolve with the server's response. See
   * `ControlChannel.request`: `options` carry the per-call deadline,
   * connection, and `AbortSignal`, and the request type's table entry
   * (`request-policy.ts`) supplies the rest.
   */
  request(input: RequestInput, options: RequestOptions = {}): Promise<ProtocolResponse> {
    return this.#channel.request(input, options);
  }

  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options: SubscribeOptions = {},
  ): Promise<EventSubscription> {
    if (this.#closed) {
      return Promise.reject(new BackendClientError('disconnected', 'Client is closed'));
    }
    return this.#openSubscription(afterSequence, onMessage, onDisconnect, options);
  }

  /** Open and own one subscription after the client-wide closed gate. */
  #openSubscription(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options: SubscribeOptions,
  ): Promise<EventSubscription> {
    return new Promise((resolve, reject) => {
      const socket = this.#network.connect(this.#path);
      const frames = new NewlineFramer();
      let subscribed = false;
      let closing = false;
      let disconnected = false;
      let protocolErrorReceived = false;
      let cancelHandshakeTimeout = (): void => {};
      let removeSocketListeners = (): void => {};
      const disconnect = (error: Error): void => {
        if (disconnected || closing) return;
        disconnected = true;
        cancelHandshakeTimeout();
        if (subscribed && protocolErrorReceived) return;
        if (subscribed) onDisconnect(error);
        else reject(error);
      };
      const rejectStartup = (error: unknown): void => {
        if (disconnected || closing) return;
        disconnected = true;
        cancelHandshakeTimeout();
        reject(error);
      };
      const failOwnedSocket = (error: Error): void => {
        if (disconnected || closing) return;
        // Socket errors need not produce a later peer-end event: fail closed now.
        this.#secondarySockets.delete(socket);
        cancelHandshakeTimeout();
        removeSocketListeners();
        socket.destroy();
        disconnect(error);
      };
      const handleStreamMessage = (message: ServerMessage): boolean => {
        if (message.type === 'protocol_error') {
          protocolErrorReceived = true;
          if (!subscribed) {
            // A structured refusal before the handshake is the server
            // declining the subscription, not the transport failing, so the
            // dial rejects with the refusal rather than with whatever the
            // ensuing close would say.
            disconnect(new ServerError(message.message, message.diagnostic ?? null));
            socket.destroy();
            return false;
          }
        }
        if (!subscribed && message.type === 'subscribed') {
          subscribed = true;
          cancelHandshakeTimeout();
          resolve({
            close: () => {
              closing = true;
              cancelHandshakeTimeout();
              this.#secondarySockets.delete(socket);
              return this.#retireSocket(socket);
            },
          });
        }
        return true;
      };
      const handleSubscriptionLine = (line: string): boolean =>
        readSubscriptionLine(line, onMessage, handleStreamMessage, failOwnedSocket);
      // Track the socket so close() can settle a pending handshake or suppress
      // the disconnect callback of a subscription that is already live.
      this.#secondarySockets.set(socket, () => {
        cancelHandshakeTimeout();
        if (subscribed) {
          closing = true;
          return;
        }
        disconnect(new BackendClientError('disconnected', 'Client closed during subscription'));
      });
      startSubscriptionSocket({
        socket,
        frames,
        timeoutMs: this.#connectTimeoutMs,
        scheduleTimeout: this.#clock.scheduleTimeout,
        callbacks: {
          onConnect: () => this.#writeSubscribe(socket, afterSequence, options),
          onFramingError: failOwnedSocket,
          onLine: handleSubscriptionLine,
          onError: error => failOwnedSocket(transportFailure(error)),
          onPeerEnd: () => {
            this.#secondarySockets.delete(socket);
            removeSocketListeners();
            disconnect(finishFraming(frames) ?? subscriptionEnded(subscribed));
          },
          isSettled: () => disconnected || closing,
          releaseOperation: () => this.#secondarySockets.delete(socket),
          rejectStartup,
          disconnect: failOwnedSocket,
          adoptListeners: remove => {
            removeSocketListeners = remove;
          },
          adoptDeadline: cancel => {
            cancelHandshakeTimeout = cancel;
          },
        },
      });
    });
  }

  /**
   * Send the subscribe request once the subscription socket connects. The frame
   * body is `subscribeRequest`'s typed union member, which owns which optional
   * fields ride along and why.
   */
  #writeSubscribe(socket: ClientSocket, afterSequence: number, options: SubscribeOptions): void {
    const request: IssuedRequest = {
      protocol_version: 1,
      request_id: globalThis.crypto.randomUUID(),
      client_id: this.#clientId,
      timestamp: new Date().toISOString(),
      ...subscribeRequest(afterSequence, options),
    };
    socket.send(`${JSON.stringify(request)}\n`);
  }

  /**
   * Redial the control channel now, whatever its backoff schedule was going to
   * do. See `ControlChannel.reconnect`.
   */
  reconnect(): void {
    this.#channel.reconnect();
  }

  /**
   * Whether the control channel currently holds a live connection. False while
   * reconnecting after an outage, and once the schedule is spent, so a frontend
   * can disable the controls a dropped channel cannot carry. `onConnectionState`
   * reports the same transitions as they happen.
   */
  get connected(): boolean {
    return this.#channel.connected;
  }

  /**
   * Close every socket the client owns: the control connection and every live
   * or opening control socket, plus every live secondary (subscription or
   * dedicated request). Cancels a pending redial, fails every request still
   * owed an answer (in flight or held for resend), and ends each live socket
   * gracefully, destroying it if it does not close within the grace window,
   * so an unresponsive server cannot hang shutdown.
   * Observer and socket failures are exposed only after every owner settles;
   * several failures retain stable control-then-socket order.
   */
  close(): Promise<void> {
    if (this.#closing !== null) return this.#closing;
    // Mark the whole client closed before failing channel work or settling
    // secondary operations, so no reentrant caller can allocate another socket.
    this.#closed = true;
    this.#closing = this.#closeOwnedSockets();
    return this.#closing;
  }

  /** Perform the one terminal teardown every close caller joins. */
  #closeOwnedSockets(): Promise<void> {
    const channelClosed = this.#channel.close();
    const openingControls = [...this.#openingControlDials.values()];
    this.#openingControlDials.clear();
    const secondaries = [...this.#secondarySockets];
    this.#secondarySockets.clear();
    for (const [, settle] of secondaries) settle();
    const secondaryRetirements = secondaries.map(([socket]) => this.#retireSocket(socket));
    // Operations that completed before close() may still be waiting for their
    // peer's FIN. Snapshot them only after every active owner has settled, so
    // this terminal close joins all teardown that can start synchronously.
    const retiring = new Set([...secondaryRetirements, ...this.#retiringSockets.values()]);
    return settleAll(
      [channelClosed, ...openingControls.map(dial => dial.close()), ...retiring],
      'could not close every server client connection',
    );
  }

  /** Begin or join the one bounded graceful retirement for a socket. */
  #retireSocket(socket: ClientSocket): Promise<void> {
    const existing = this.#retiringSockets.get(socket);
    if (existing !== undefined) return existing;
    const retiring = closeSocketWithin(
      socket,
      this.#closeGraceMs,
      this.#clock.scheduleTimeout,
    ).finally(() => {
      if (this.#retiringSockets.get(socket) === retiring) {
        this.#retiringSockets.delete(socket);
      }
    });
    this.#retiringSockets.set(socket, retiring);
    return retiring;
  }

  /** Dial one control socket for the channel. */
  async #dialControl(handlers: ControlConnectionHandlers): Promise<ControlConnection> {
    const dial = createSocketDial(
      this.#path,
      this.#connectTimeoutMs,
      `Timed out connecting to server after ${this.#connectTimeoutMs}ms`,
      this.#clock.scheduleTimeout,
      this.#network,
    );
    // Registration is synchronous with socket creation, before the first
    // await, so close() cannot miss a connection attempt already in progress.
    this.#openingControlDials.set(dial.socket, dial);
    // Scheduling and listener registration are external calls and may re-enter
    // `close()`. Publish the dial owner first so that close can cancel and join
    // this socket even when that re-entry happens inside `start()`.
    dial.start();
    let socket: ClientSocket;
    try {
      socket = await dial.connected;
    } catch (error) {
      this.#openingControlDials.delete(dial.socket);
      throw error;
    }
    if (!this.#openingControlDials.has(socket)) {
      throw new BackendClientError('disconnected', 'Client closed during control connection');
    }
    let connection: ControlConnection;
    try {
      connection = this.#controlConnection(socket, handlers);
    } catch (error) {
      this.#openingControlDials.delete(socket);
      await dial.close().catch(() => undefined);
      throw error;
    }
    // Exactly one owner claims the connected socket. If close() cleared the
    // set first, it also destroyed and joined the socket, so the channel must
    // not adopt it afterward.
    if (!this.#openingControlDials.delete(socket)) {
      await connection.close().catch(() => undefined);
      throw new BackendClientError('disconnected', 'Client closed during control connection');
    }
    return connection;
  }

  /**
   * Wrap one connected socket as a control connection: a framer of its own, so
   * a superseded socket's trailing bytes can never splice into the live
   * socket's stream, and every failure routed to the channel rather than
   * thrown at whoever happened to be writing.
   */
  #controlConnection(socket: ClientSocket, handlers: ControlConnectionHandlers): ControlConnection {
    const frames = new NewlineFramer();
    const listeners = acquireSocketListeners(
      () => socket.setTextMode(),
      () =>
        socket.listenData(chunk => {
          const lines = frameChunk(frames, chunk, handlers.onFault);
          if (lines === null) return;
          for (const line of lines) {
            if (line) handlers.onFrame(line);
          }
        }),
      () => socket.listenError(error => handlers.onDrop(transportFailure(error))),
      () =>
        socket.listenPeerEnd(() => {
          const framing = finishFraming(frames);
          if (framing === null) {
            handlers.onDrop(new BackendClientError('disconnected', 'Server disconnected'));
          } else {
            handlers.onFault(framing);
          }
        }),
    );
    if (!listeners.ok) throw listeners.error;
    let closing: Promise<void> | null = null;
    return {
      send: frame =>
        socket.send(`${frame}\n`, error => {
          // A write failure means the socket is broken; report it as a drop so
          // the request is disposed by its policy and the redial loop takes over.
          if (error) handlers.onDrop(transportFailure(error));
        }),
      close: () => {
        if (closing !== null) return closing;
        // Keep an error sink across the gap between releasing the live
        // connection listeners and terminal socket close.
        const retirementError = socketListenerResult(() => socket.listenError(() => undefined));
        listeners.remove();
        if (!retirementError.ok) {
          closing = destroySocket(socket).then(
            () => {
              throw retirementError.error;
            },
            () => {
              throw retirementError.error;
            },
          );
          return closing;
        }
        closing = this.#retireSocket(socket).then(
          () => retirementError.remove(),
          error => {
            retirementError.remove();
            throw error;
          },
        );
        return closing;
      },
    };
  }

  /**
   * Run one request on its own connection without a response timer.
   *
   * Chat duration is bounded by the configured agent, not by the control RPC
   * timeout. A dedicated connection also prevents a long chat from blocking
   * pause, resume, and snapshot requests in the server's per-connection loop. An
   * `AbortSignal` cancels it: the socket is torn down and the promise rejects
   * with the abort reason.
   */
  #requestLongRunning(request: IssuedRequest, signal?: AbortSignalLike): Promise<ProtocolResponse> {
    return new Promise((resolve, reject) => {
      const socket = this.#network.connect(this.#path);
      // A client-wide close() abandons an in-flight chat: fail it as a
      // disconnect and let close() destroy the socket.
      this.#secondarySockets.set(socket, () => {
        fail(new BackendClientError('disconnected', 'Client closed during chat'));
      });
      const frames = new NewlineFramer();
      let settled = false;
      let detachAbort = (): void => {};
      let cancelConnectTimeout = (): void => {};
      let removeSocketListeners = (): void => {};

      const cleanup = (): void => {
        cancelConnectTimeout();
        detachAbort();
        this.#secondarySockets.delete(socket);
        removeSocketListeners();
      };
      const rejectOwned = (error: unknown): void => {
        if (settled) return;
        settled = true;
        cleanup();
        socket.destroy();
        reject(error);
      };
      const fail = (error: Error): void => {
        rejectOwned(error instanceof BackendClientError ? error : transportFailure(error));
      };
      const abandon = (): void => {
        if (signal !== undefined) rejectOwned(abortReason(signal));
      };
      const finish = (response: ProtocolResponse): void => {
        if (settled) return;
        // Install the retirement error sink before removing the request's
        // listeners, so a peer reset during FIN can never become unhandled.
        const retirementError = socketListenerResult(() => socket.listenError(() => undefined));
        if (!retirementError.ok) {
          rejectOwned(retirementError.error);
          return;
        }
        settled = true;
        cleanup();
        const retirement = this.#retireSocket(socket);
        // The retirement promise already joins the socket's close event. Use
        // that owner to release the error sink instead of acquiring a second,
        // structurally fallible close listener solely for cleanup.
        void retirement.then(retirementError.remove, retirementError.remove);
        if (response.ok) resolve(response);
        else reject(responseError(response));
      };

      startDedicatedSocket({
        socket,
        request,
        frames,
        signal,
        scheduleTimeout: this.#clock.scheduleTimeout,
        timeoutMs: this.#connectTimeoutMs,
        callbacks: {
          isSettled: () => settled,
          adoptAbort: detach => {
            detachAbort = detach;
          },
          adoptDeadline: cancel => {
            cancelConnectTimeout = cancel;
          },
          adoptListeners: remove => {
            removeSocketListeners = remove;
          },
          abandon,
          rejectOwned,
          fail,
          finish,
        },
      });
    });
  }
}

/** One socket attempt, synchronously ownable before its connection settles. */
interface SocketDial {
  readonly socket: ClientSocket;
  readonly connected: Promise<ClientSocket>;
  /** Acquire the deadline and socket listeners after the caller publishes ownership. */
  start(): void;
  /** Cancel its deadline, reject the attempt, destroy the socket, and join it. */
  close(): Promise<void>;
}

/**
 * Create one server-socket dial and return its ownership handle before start.
 * This is the one boundary that knows Node errnos, so everything downstream
 * branches on `kind`/`retryable`, never on the code.
 */
function createSocketDial(
  path: string,
  timeoutMs: number,
  timeoutMessage: string,
  scheduleTimeout: ScheduleTimeout,
  network: ClientNetwork,
): SocketDial {
  const socket = network.connect(path);
  let retirement: Promise<void> | null = null;
  const retire = (): Promise<void> => {
    retirement ??= destroySocket(socket);
    return retirement;
  };
  let resolveConnected = (_socket: ClientSocket): void => {};
  let rejectConnected = (_error: unknown): void => {};
  const connected = new Promise<ClientSocket>((resolve, reject) => {
    resolveConnected = resolve;
    rejectConnected = reject;
  });
  let started = false;
  let settled = false;
  let cancelTimeout = (): void => {};
  let removeConnectListener: RemoveSocketListener = () => {};
  let removeErrorListener: RemoveSocketListener = () => {};
  const cleanup = (): void => {
    cancelTimeout();
    removeConnectListener();
    removeErrorListener();
  };
  const rejectOwned = (error: unknown): void => {
    if (settled) return;
    settled = true;
    cleanup();
    // Creation transferred the socket to this dial before any fallible
    // scheduler or listener acquisition. Preserve that initiating failure,
    // but expose it only after the socket's terminal close has been joined.
    void retire().then(
      () => rejectConnected(error),
      () => rejectConnected(error),
    );
  };
  const fail = (error: BackendClientError): void => {
    rejectOwned(error);
  };
  const onError = (error: Error): void => {
    fail(dialFailure(error));
  };
  const onConnect = (): void => {
    if (settled) return;
    settled = true;
    cleanup();
    resolveConnected(socket);
  };
  const start = (): void => {
    if (started || settled) return;
    started = true;
    const deadline = scheduleResult(scheduleTimeout, timeoutMs, () => {
      fail(new BackendClientError('timeout', timeoutMessage));
    });
    if (!deadline.ok) {
      rejectOwned(deadline.error);
      return;
    }
    cancelTimeout = deadline.cancel;
    // The scheduler may deliver while returning its cancellation handle. The
    // timeout already owns retirement, but this frame still owns the handle.
    if (settled) {
      cancelTimeout();
      return;
    }

    const connectListener = socketListenerResult(() => socket.listenConnect(onConnect));
    if (!connectListener.ok) {
      rejectOwned(connectListener.error);
      return;
    }
    removeConnectListener = connectListener.remove;
    // A Fake or another conforming implementation may report an already-open
    // socket during registration. Adopt and remove the returned handle before
    // publishing the connection; there is no second listener to acquire then.
    if (settled) {
      removeConnectListener();
      return;
    }

    const errorListener = socketListenerResult(() => socket.listenError(onError));
    if (!errorListener.ok) {
      rejectOwned(errorListener.error);
      return;
    }
    removeErrorListener = errorListener.remove;
    if (settled) removeErrorListener();
  };
  return {
    socket,
    connected,
    start,
    close: () => {
      fail(new BackendClientError('disconnected', 'Client closed during control connection'));
      return retire();
    },
  };
}

/** Destroy a not-yet-claimed socket and join its actual close event. */
function destroySocket(socket: ClientSocket): Promise<void> {
  return new Promise((resolve, reject) => {
    if (socket.closed) {
      resolve();
      return;
    }
    const closeListener = socketListenerResult(() => socket.listenClose(resolve));
    if (!closeListener.ok) {
      // The adapter cannot report terminal close, but it must still receive the
      // destroy before its structural failure is surfaced to the owner.
      try {
        socket.destroy();
      } finally {
        reject(closeListener.error);
      }
      return;
    }
    socket.destroy();
  });
}

/**
 * Classify one dial attempt's failure: only the errors a still-starting server
 * produces are transient. Everything downstream branches on `kind` and
 * `retryable`, never on the code.
 */
function dialFailure(error: Error): BackendClientError {
  const code = (error as {code?: string}).code;
  return new BackendClientError('disconnected', error.message, {
    retryable: code !== undefined && RETRYABLE_CONNECT_CODES.has(code),
    cause: error,
  });
}

function isTransientDialError(error: unknown): error is BackendClientError {
  return error instanceof BackendClientError && error.kind === 'disconnected' && error.retryable;
}

/** A live connection failed under an operation; the server said nothing. */
function transportFailure(error: Error): BackendClientError {
  return new BackendClientError('disconnected', error.message, {cause: error});
}

/**
 * Frame one socket chunk, or report an unreadable stream. Returns the completed
 * lines, or null when the framer rejects the chunk (an oversized newline-less
 * remainder), having first handed the typed failure to `onFramingError` so the
 * caller can tear its connection down.
 */
function frameChunk(
  framer: NewlineFramer,
  chunk: string,
  onFramingError: (error: BackendClientError) => void,
): string[] | null {
  try {
    return framer.push(chunk);
  } catch (error) {
    onFramingError(streamFailure(error));
    return null;
  }
}

/** Finalize one newline-framed peer stream without throwing from its terminal event. */
function finishFraming(framer: NewlineFramer): BackendClientError | null {
  try {
    framer.finish();
    return null;
  } catch (error) {
    return streamFailure(error);
  }
}

function subscriptionEnded(subscribed: boolean): BackendClientError {
  return new BackendClientError(
    'disconnected',
    subscribed
      ? 'Server event stream disconnected'
      : 'Server event stream disconnected before subscription',
  );
}

function chatEnded(): BackendClientError {
  return new BackendClientError('disconnected', 'Server disconnected during chat');
}

function delay(ms: number, scheduleTimeout: ScheduleTimeout): Promise<void> {
  return new Promise(resolve => scheduleTimeout(resolve, ms));
}

interface SubscriptionSocketCallbacks {
  onConnect(): void;
  onFramingError(error: BackendClientError): void;
  onLine(line: string): boolean;
  onError(error: Error): void;
  onPeerEnd(): void;
}

/** Parse and deliver one event-stream line without throwing from socket I/O. */
function readSubscriptionLine(
  line: string,
  onMessage: (message: ServerMessage) => void,
  handleMessage: (message: ServerMessage) => boolean,
  fail: (error: Error) => void,
): boolean {
  if (!line) return true;
  try {
    const message = parseServerMessage(line);
    onMessage(message);
    return handleMessage(message);
  } catch (error) {
    fail(streamFailure(error));
    return false;
  }
}

interface SubscriptionStartupCallbacks extends SubscriptionSocketCallbacks {
  isSettled(): boolean;
  releaseOperation(): void;
  rejectStartup(error: unknown): void;
  disconnect(error: Error): void;
  adoptListeners(remove: RemoveSocketListener): void;
  adoptDeadline(cancel: () => void): void;
}

interface SubscriptionSocketStartup {
  readonly socket: ClientSocket;
  readonly frames: NewlineFramer;
  readonly timeoutMs: number;
  readonly scheduleTimeout: ScheduleTimeout;
  readonly callbacks: SubscriptionStartupCallbacks;
}

/** Acquire one subscription's listeners and deadline as one owned startup. */
function startSubscriptionSocket(startup: SubscriptionSocketStartup): void {
  const {socket, frames, timeoutMs, scheduleTimeout, callbacks} = startup;
  const listeners = listenSubscriptionSocket(socket, frames, callbacks);
  if (!listeners.ok) {
    callbacks.releaseOperation();
    callbacks.rejectStartup(listeners.error);
    socket.destroy();
    return;
  }
  callbacks.adoptListeners(listeners.remove);
  // A conforming implementation may deliver a terminal event while its
  // listener is being acquired. Adopt its cleanup before checking settlement.
  if (callbacks.isSettled()) {
    listeners.remove();
    socket.destroy();
    return;
  }
  const deadline = scheduleSubscriptionDeadline(
    socket,
    timeoutMs,
    scheduleTimeout,
    callbacks.disconnect,
  );
  if (!deadline.ok) {
    callbacks.releaseOperation();
    listeners.remove();
    callbacks.rejectStartup(deadline.error);
    socket.destroy();
    return;
  }
  callbacks.adoptDeadline(deadline.cancel);
  // The scheduler may synchronously deliver before returning its owner.
  if (callbacks.isSettled()) deadline.cancel();
}

/** Acquire one subscription's complete listener set as a transaction. */
function listenSubscriptionSocket(
  socket: ClientSocket,
  frames: NewlineFramer,
  callbacks: SubscriptionSocketCallbacks,
): SocketListenerResult {
  return acquireSocketListeners(
    () => socket.setTextMode(),
    () => socket.listenConnect(callbacks.onConnect),
    () =>
      socket.listenData(chunk => {
        const lines = frameChunk(frames, chunk, callbacks.onFramingError);
        if (lines === null) return;
        for (const line of lines) {
          if (!callbacks.onLine(line)) return;
        }
      }),
    () => socket.listenError(callbacks.onError),
    () => socket.listenPeerEnd(callbacks.onPeerEnd),
  );
}

interface DedicatedSocketCallbacks {
  cancelConnectTimeout(): void;
  fail(error: Error): void;
  finish(response: ProtocolResponse): void;
}

interface DedicatedStartupCallbacks {
  isSettled(): boolean;
  adoptAbort(detach: () => void): void;
  adoptDeadline(cancel: () => void): void;
  adoptListeners(remove: RemoveSocketListener): void;
  abandon(): void;
  rejectOwned(error: unknown): void;
  fail(error: Error): void;
  finish(response: ProtocolResponse): void;
}

interface DedicatedSocketStartup {
  readonly socket: ClientSocket;
  readonly request: IssuedRequest;
  readonly frames: NewlineFramer;
  readonly signal: AbortSignalLike | undefined;
  readonly scheduleTimeout: ScheduleTimeout;
  readonly timeoutMs: number;
  readonly callbacks: DedicatedStartupCallbacks;
}

/** Acquire every owner a dedicated request needs, rolling back at the first failure. */
function startDedicatedSocket(startup: DedicatedSocketStartup): void {
  const {socket, request, frames, signal, scheduleTimeout, timeoutMs, callbacks} = startup;
  if (signal !== undefined) {
    const registered = abortRegistrationResult(signal, callbacks.abandon);
    if (!registered.ok) {
      callbacks.rejectOwned(registered.error);
      return;
    }
    callbacks.adoptAbort(registered.registration.detach);
    // Listener registration can re-enter client close. Adopt the returned
    // cleanup even when that close already settled the request.
    if (callbacks.isSettled()) {
      registered.registration.detach();
      return;
    }
    if (registered.registration.aborted) {
      callbacks.abandon();
      return;
    }
  }
  const deadline = scheduleResult(scheduleTimeout, timeoutMs, () =>
    callbacks.fail(
      new BackendClientError('timeout', `Timed out connecting to server after ${timeoutMs}ms`),
    ),
  );
  if (!deadline.ok) {
    callbacks.rejectOwned(deadline.error);
    return;
  }
  callbacks.adoptDeadline(deadline.cancel);
  if (callbacks.isSettled()) {
    deadline.cancel();
    return;
  }

  const listeners = listenDedicatedSocket(socket, request, frames, {
    cancelConnectTimeout: deadline.cancel,
    fail: callbacks.fail,
    finish: callbacks.finish,
  });
  if (!listeners.ok) {
    callbacks.rejectOwned(listeners.error);
    return;
  }
  callbacks.adoptListeners(listeners.remove);
  if (callbacks.isSettled()) listeners.remove();
}

/** Attach one dedicated request's wire behavior and return its full listener cleanup. */
function listenDedicatedSocket(
  socket: ClientSocket,
  request: IssuedRequest,
  frames: NewlineFramer,
  callbacks: DedicatedSocketCallbacks,
): SocketListenerResult {
  return acquireSocketListeners(
    () => socket.setTextMode(),
    () =>
      socket.listenConnect(() => {
        callbacks.cancelConnectTimeout();
        socket.send(`${JSON.stringify(request)}\n`, error => {
          if (error) callbacks.fail(error);
        });
      }),
    () =>
      socket.listenData(chunk => {
        const lines = frameChunk(frames, chunk, callbacks.fail);
        if (lines === null) return;
        for (const line of lines) {
          const response = dedicatedResponse(line, request.request_id);
          if (response === null) continue;
          if (response instanceof Error) callbacks.fail(response);
          else callbacks.finish(response);
          return;
        }
      }),
    () => socket.listenError(callbacks.fail),
    () => socket.listenPeerEnd(() => callbacks.fail(finishFraming(frames) ?? chatEnded())),
  );
}

/** Parse the one response a dedicated socket owes, preserving protocol failures as values. */
function dedicatedResponse(line: string, requestId: string): ProtocolResponse | Error | null {
  if (!line) return null;
  try {
    const response = parseProtocolResponse(line);
    if (response.request_id === requestId) return response;
    return new BackendClientError('parse', 'Server chat response has an unexpected request ID');
  } catch (error) {
    return toError(error);
  }
}

type ScheduleResult =
  | {readonly ok: true; readonly cancel: () => void}
  | {readonly ok: false; readonly error: unknown};

/** Call the injected scheduler without losing a synchronous implementation failure. */
function scheduleResult(
  scheduleTimeout: ScheduleTimeout,
  delayMs: number,
  callback: () => void,
): ScheduleResult {
  try {
    return {ok: true, cancel: scheduleTimeout(callback, delayMs)};
  } catch (error) {
    return {ok: false, error};
  }
}

type SocketListenerResult =
  | {readonly ok: true; readonly remove: RemoveSocketListener}
  | {readonly ok: false; readonly error: unknown};

/** Acquire one external listener without letting a structural throw orphan its socket. */
function socketListenerResult(register: () => RemoveSocketListener): SocketListenerResult {
  try {
    return {ok: true, remove: register()};
  } catch (error) {
    return {ok: false, error};
  }
}

/** Acquire a socket's setup and listeners as one rollback-safe transaction. */
function acquireSocketListeners(
  prepare: () => void,
  ...register: ReadonlyArray<() => RemoveSocketListener>
): SocketListenerResult {
  const acquired: RemoveSocketListener[] = [];
  const remove = (): void => {
    for (const release of acquired.splice(0).reverse()) release();
  };
  try {
    prepare();
    for (const listener of register) acquired.push(listener());
    return {ok: true, remove};
  } catch (error) {
    remove();
    return {ok: false, error};
  }
}

/** Acquire the subscription deadline after its socket has one terminal owner. */
function scheduleSubscriptionDeadline(
  socket: ClientSocket,
  timeoutMs: number,
  scheduleTimeout: ScheduleTimeout,
  disconnect: (error: Error) => void,
): ScheduleResult {
  return scheduleResult(scheduleTimeout, timeoutMs, () => {
    disconnect(
      new BackendClientError('timeout', `Server subscription timed out after ${timeoutMs}ms`),
    );
    socket.destroy();
  });
}

type AbortRegistrationResult =
  | {readonly ok: true; readonly registration: AbortRegistration}
  | {readonly ok: false; readonly error: unknown};

/** Keep a structural listener-registration failure as an owned outcome. */
function abortRegistrationResult(
  signal: AbortSignalLike,
  onAbort: () => void,
): AbortRegistrationResult {
  try {
    return {ok: true, registration: registerAbort(signal, onAbort)};
  } catch (error) {
    return {ok: false, error};
  }
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

/**
 * End a socket gracefully, then force it closed if the peer does not complete
 * the FIN handshake within `graceMs`. Resolves once the socket is actually
 * closed, so an unresponsive server cannot hang its owner's shutdown.
 */
function closeSocketWithin(
  socket: ClientSocket,
  graceMs: number,
  scheduleTimeout: ScheduleTimeout,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (socket.closed) return resolve();
    let settled = false;
    let failure: {readonly error: unknown} | null = null;
    let cancelTimeout = (): void => {};
    let removeCloseListener: RemoveSocketListener = () => {};
    const finish = (): void => {
      if (settled) return;
      settled = true;
      cancelTimeout();
      removeCloseListener();
      if (failure === null) resolve();
      else reject(failure.error);
    };
    const closeListener = socketListenerResult(() => socket.listenClose(finish));
    if (!closeListener.ok) {
      // There is no listener to join after structural acquisition failed, but
      // this owner must still fail closed before preserving that exact error.
      try {
        socket.destroy();
      } finally {
        reject(closeListener.error);
      }
      return;
    }
    removeCloseListener = closeListener.remove;
    if (settled) {
      removeCloseListener();
      return;
    }

    const deadline = scheduleResult(scheduleTimeout, graceMs, () => socket.destroy());
    if (!deadline.ok) {
      failure = {error: deadline.error};
      socket.destroy();
      return;
    }
    cancelTimeout = deadline.cancel;
    // Immediate delivery may have synchronously closed the socket before the
    // scheduler returned the timer owner. Adopt and cancel that handle now.
    if (settled) {
      cancelTimeout();
      return;
    }
    try {
      socket.end();
    } catch (error) {
      failure = {error};
      socket.destroy();
    }
  });
}
