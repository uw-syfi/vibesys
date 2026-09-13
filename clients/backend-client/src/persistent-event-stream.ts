import type {EventSubscription, SubscribeOptions} from './client.js';
import type {ServerMessage} from './protocol.js';

/**
 * The slice of `ServerClient` a persistent stream drives. Both the production
 * client and any transport the caller wraps around it (a tui `ServerTransport`,
 * a test fake) satisfy this structurally, so the abstraction lives here in
 * `backend-client` without depending on anything above it.
 */
export interface StreamTransport {
  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<EventSubscription>;
}

/** Whether the stream currently holds a live subscription. */
export type StreamConnectionState =
  | {readonly status: 'connected'}
  | {readonly status: 'disconnected'; readonly error: Error};

export interface PersistentEventStreamCallbacks {
  /**
   * The sequence to resume after: the last event the caller has folded. Read
   * fresh on every reconnect, so a resume asks for exactly what the outage may
   * have swallowed and nothing the caller already holds.
   */
  cursor(): number;
  /**
   * The store the cursor belongs to, as the caller last saw it named. Carried
   * on a resume so the server can tell whether that cursor still numbers the
   * live store; empty until the caller has seen one, in which case the resume
   * is a plain cursor resume.
   */
  storeId(): string;
  /**
   * Whether a dropped stream is worth redialing. A finished run or a protocol
   * error has nothing more to stream, so the drop is lifecycle cleanup rather
   * than an outage, and the stream stays down without a banner.
   */
  shouldReconnect(): boolean;
  /**
   * Every `ServerMessage` the subscription delivers, tagged with whether it
   * came from a resume (from the caller's cursor) or a bootstrap (a fresh tail).
   * The flag is bound when the subscription is dialed, so a batch delivered
   * synchronously, before `subscribe` resolves, is tagged correctly too.
   */
  onMessage(message: ServerMessage, context: {readonly resumed: boolean}): void;
  /**
   * `disconnected` when a live stream drops or a dial fails outright;
   * `connected` when a reconnect recovers. Not emitted on the first successful
   * boot: the stream is connected by default and nothing changed.
   */
  onConnectionState(state: StreamConnectionState): void;
}

export interface PersistentEventStreamOptions {
  /**
   * Replay at most this many of the newest events on the boot subscribe. When
   * set, the boot dials with the tail and falls back to a full replay if the
   * server rejects the field; when omitted, the boot is a single full replay.
   */
  tail?: number;
  /**
   * Backoff between reconnect attempts after the stream drops. The schedule is
   * finite: a server that refuses this many dials in a row is not coming back
   * on its own, and the disconnect stays as the persistent answer. A successful
   * reconnect resets the count, so the next outage gets the full schedule.
   */
  reconnectDelaysMs?: readonly number[];
}

const DEFAULT_RECONNECT_DELAYS_MS: readonly number[] = [500, 1_000, 2_000, 4_000, 8_000];

function toError(value: unknown): Error {
  return value instanceof Error ? value : new Error(String(value));
}

/**
 * Owns the dial/redial loop for a `ServerClient` subscription: it bootstraps
 * once, watches for the socket to drop, and resubscribes from the caller's
 * cursor on a finite backoff. The caller supplies where to resume from, whether
 * a drop is worth reconnecting, and where messages and connection changes go;
 * it never touches a subscription handle or a reconnect timer.
 *
 * The resume-versus-bootstrap decision is internal: until a bootstrap batch has
 * landed there is no cursor to resume from, so a drop before the first batch
 * re-bootstraps (tail fallback and all) rather than resuming from nothing.
 */
export class PersistentEventStream {
  readonly #transport: StreamTransport;
  readonly #tail: number | undefined;
  readonly #reconnectDelaysMs: readonly number[];

  #callbacks: PersistentEventStreamCallbacks | null = null;
  #subscription: EventSubscription | null = null;
  #reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  #reconnectAttempt = 0;
  /**
   * Identifies the live dial. Each subscribe attempt takes the next value, so a
   * message or disconnect from a subscription the loop has already moved past
   * fails the check and is ignored: a stale socket cannot schedule a reconnect
   * or deliver state after the stream moved on.
   */
  #connectionSeq = 0;
  /** True once a bootstrap batch has landed, so a resume has a cursor to use. */
  #bootstrapped = false;
  #closed = false;

  constructor(transport: StreamTransport, options: PersistentEventStreamOptions = {}) {
    this.#transport = transport;
    this.#tail = options.tail;
    this.#reconnectDelaysMs = options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS;
  }

  /**
   * Bootstraps the subscription. Resolves once the boot dial settles, whether
   * it connected or reported its failure; a boot failure does not arm the
   * reconnect loop, since only a stream that once connected can drop.
   */
  async subscribe(callbacks: PersistentEventStreamCallbacks): Promise<void> {
    if (this.#callbacks !== null) throw new Error('PersistentEventStream is already subscribed');
    this.#callbacks = callbacks;
    await this.#bootstrapDial();
  }

  /**
   * The callbacks, which every dial and delivery path reaches only after
   * `subscribe` has set them; the guard is for a caller that wires the loop
   * itself, which nothing here does.
   */
  #active(): PersistentEventStreamCallbacks {
    if (this.#callbacks === null) throw new Error('PersistentEventStream is not subscribed');
    return this.#callbacks;
  }

  /**
   * Tears the stream down. Cancels a pending reconnect and closes the live
   * subscription; a dial still in flight closes itself when it resolves, since
   * a not-yet-resolved subscribe is not there to cancel.
   */
  async close(): Promise<void> {
    this.#closed = true;
    if (this.#reconnectTimer !== null) {
      clearTimeout(this.#reconnectTimer);
      this.#reconnectTimer = null;
    }
    const subscription = this.#subscription;
    this.#subscription = null;
    await subscription?.close();
  }

  /**
   * Subscribes from sequence 0. With a tail configured it probes for the field
   * and falls back to a full replay, since a server that predates `tail`
   * rejects it; the fallback's own failure is the one reported, so one boot
   * never raises two banners.
   */
  async #bootstrapDial(): Promise<boolean> {
    if (this.#tail !== undefined) {
      try {
        return await this.#dial(0, false, {tail: this.#tail});
      } catch {
        // Expected against a server without `tail`; fall through to full replay.
      }
    }
    try {
      return await this.#dial(0, false);
    } catch (error) {
      this.#emit({status: 'disconnected', error: toError(error)});
      return false;
    }
  }

  /**
   * Resubscribes from the caller's cursor with no tail, naming the store that
   * cursor belongs to so the server drops it if the store was swapped while the
   * stream was down. A server that predates the field rejects it, so the known
   * store falls back to a plain cursor resume rather than failing the reconnect.
   * A failure is otherwise silent: the disconnect banner is already up and
   * accurate, and the next attempt, if the schedule has one, speaks for itself.
   */
  async #resumeDial(): Promise<boolean> {
    const cursor = this.#active().cursor();
    const storeId = this.#active().storeId();
    if (storeId) {
      try {
        return await this.#dial(cursor, true, {storeId});
      } catch {
        // Expected against a server without `store_id` on subscribe; fall
        // through to a cursor-only resume.
      }
    }
    try {
      return await this.#dial(cursor, true);
    } catch {
      return false;
    }
  }

  /**
   * One subscribe attempt. Binds the resume flag and the connection identity
   * into the message and disconnect handlers before dialing, so a batch the
   * server delivers inside `subscribe` (before the promise resolves) is tagged
   * and attributed correctly.
   */
  async #dial(
    afterSequence: number,
    resumed: boolean,
    options?: SubscribeOptions,
  ): Promise<boolean> {
    const token = ++this.#connectionSeq;
    const subscription = await this.#transport.subscribe(
      afterSequence,
      message => this.#deliver(message, resumed, token),
      error => this.#handleDisconnect(error, token),
      options,
    );
    if (this.#closed) {
      await subscription.close();
      return false;
    }
    this.#subscription = subscription;
    return true;
  }

  #deliver(message: ServerMessage, resumed: boolean, token: number): void {
    if (this.#closed || token !== this.#connectionSeq) return;
    if (!resumed && message.type === 'event_batch') this.#bootstrapped = true;
    this.#active().onMessage(message, {resumed});
  }

  #handleDisconnect(error: Error, token: number): void {
    // A stale subscription's late close is not this stream's outage, and a run
    // that finished or hit a protocol error has nothing left to stream.
    if (this.#closed || token !== this.#connectionSeq) return;
    if (!this.#active().shouldReconnect()) return;
    this.#emit({status: 'disconnected', error});
    this.#scheduleReconnect();
  }

  #scheduleReconnect(): void {
    if (this.#reconnectTimer !== null) return;
    const delay = this.#reconnectDelaysMs[this.#reconnectAttempt];
    if (delay === undefined) return;
    this.#reconnectAttempt += 1;
    this.#reconnectTimer = setTimeout(() => {
      this.#reconnectTimer = null;
      void this.#reconnectNow();
    }, delay);
  }

  async #reconnectNow(): Promise<void> {
    if (this.#closed || !this.#active().shouldReconnect()) return;
    const stale = this.#subscription;
    this.#subscription = null;
    try {
      await stale?.close();
    } catch {
      // The subscription is already dead; closing it owes nothing.
    }
    const recovered = this.#bootstrapped ? await this.#resumeDial() : await this.#bootstrapDial();
    if (this.#closed) return;
    if (recovered) {
      this.#reconnectAttempt = 0;
      this.#emit({status: 'connected'});
    } else {
      this.#scheduleReconnect();
    }
  }

  #emit(state: StreamConnectionState): void {
    this.#active().onConnectionState(state);
  }
}
