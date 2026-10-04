import {BackoffSchedule, DEFAULT_RECONNECT_DELAYS_MS} from './backoff.js';
import {defaultScheduleTimeout, type ScheduleTimeout} from './control-channel.js';
import {isServerRejection} from './errors.js';
import type {ServerMessage} from './protocol.js';
import type {EventSubscription, SubscribeOptions} from './transport.js';

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
   * error has nothing more to stream, so the stream stays down rather than
   * dialing a peer that will never send another event.
   *
   * This decides redialing, not reporting. Declining a redial silences only
   * the drops that cost nothing: once the bootstrap has landed the caller
   * holds the history it asked for, so the socket closing behind it says
   * nothing new. A drop before the bootstrap is reported either way, because
   * the caller's fold is then empty through no fault of the run, and a caller
   * that is never told cannot tell that apart from a run with no events.
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
   *
   * A drop before the bootstrap is reported even when `shouldReconnect`
   * declines the redial, so `disconnected` is not a promise that a `connected`
   * will follow.
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
  /** Timer seam for reconnect backoff; tests inject a deterministic scheduler. */
  scheduleTimeout?: ScheduleTimeout;
}

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
  readonly #backoff: BackoffSchedule;
  readonly #scheduleTimeout: ScheduleTimeout;

  #callbacks: PersistentEventStreamCallbacks | null = null;
  #subscription: EventSubscription | null = null;
  #cancelReconnect: (() => void) | null = null;
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
  /**
   * Whether the caller currently sees the stream as disconnected. A single
   * outage can fail many dials in a row; this reports `disconnected` once, on
   * the transition, instead of once per failed attempt. Cleared when a
   * reconnect recovers, so the next outage reports again.
   */
  #disconnectedReported = false;
  /** Guards `#reconnectNow` so a manual `retry()` cannot stack a second dial. */
  #reconnecting = false;

  constructor(transport: StreamTransport, options: PersistentEventStreamOptions = {}) {
    this.#transport = transport;
    this.#tail = options.tail;
    this.#backoff = new BackoffSchedule(options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS);
    this.#scheduleTimeout = options.scheduleTimeout ?? defaultScheduleTimeout;
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
    if (this.#cancelReconnect !== null) {
      this.#cancelReconnect();
      this.#cancelReconnect = null;
    }
    const subscription = this.#subscription;
    this.#subscription = null;
    await subscription?.close();
  }

  /**
   * Subscribes from sequence 0. With a tail configured it probes for the field
   * and falls back to a full replay only when the server rejects it, since
   * that is what a server that predates `tail` does; the fallback's own
   * failure is the one reported, so one boot never raises two banners. A
   * transport failure is not a verdict on the field: it is reported as the
   * outage it is, and the next attempt, if the caller's loop has one, probes
   * with the tail intact.
   */
  async #bootstrapDial(): Promise<boolean> {
    if (this.#tail !== undefined) {
      try {
        return await this.#dial(0, false, {tail: this.#tail});
      } catch (error) {
        if (!isServerRejection(error)) {
          this.#reportDisconnected(toError(error));
          return false;
        }
        // The server refused `tail`; fall through to a full replay.
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
   * Only that explicit rejection downgrades the resume: a transport failure
   * says nothing about the field, and dropping the store name on one would let
   * a swapped store accept the stale cursor, so the attempt just fails and the
   * schedule retries with the store intact. A failure is otherwise silent: the
   * disconnect banner is already up and accurate, and the next attempt, if the
   * schedule has one, speaks for itself.
   */
  async #resumeDial(): Promise<boolean> {
    const cursor = this.#active().cursor();
    const storeId = this.#active().storeId();
    if (storeId) {
      try {
        return await this.#dial(cursor, true, {storeId});
      } catch (error) {
        if (!isServerRejection(error)) return false;
        // The server refused `store_id`; fall through to a cursor-only resume.
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

  /**
   * A live subscription dropped: two decisions, taken in that order.
   *
   * Whether to redial is the caller's, and unchanged. Whether to report is not,
   * and used to be the same answer, which is the defect: silence is only right
   * when the drop cost the caller nothing, and a declined redial is not enough
   * to establish that. Before the bootstrap batch lands the caller holds none
   * of the history the stream was dialed for, so a drop then leaves it with an
   * empty fold and no way to know the fold is empty because the stream failed.
   * A finished run reached through a snapshot is exactly that case, and it
   * rendered a terminal status over a blank transcript (#1044).
   *
   * After the bootstrap, a drop the caller declines to redial is the socket
   * closing behind history the caller already has, with nothing more coming:
   * silent, as before.
   */
  #handleDisconnect(error: Error, token: number): void {
    // A stale subscription's late close is not this stream's outage.
    if (this.#closed || token !== this.#connectionSeq) return;
    const redialing = this.#active().shouldReconnect();
    if (redialing || !this.#bootstrapped) this.#reportDisconnected(error);
    if (redialing) this.#scheduleReconnect();
  }

  #scheduleReconnect(): void {
    if (this.#cancelReconnect !== null) return;
    const delay = this.#backoff.next();
    if (delay === undefined) return;
    this.#cancelReconnect = this.#scheduleTimeout(() => {
      this.#cancelReconnect = null;
      void this.#reconnectNow();
    }, delay);
  }

  async #reconnectNow(): Promise<void> {
    if (this.#reconnecting) return;
    if (this.#closed || !this.#active().shouldReconnect()) return;
    this.#reconnecting = true;
    try {
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
        this.#backoff.reset();
        this.#disconnectedReported = false;
        this.#emit({status: 'connected'});
      } else {
        this.#scheduleReconnect();
      }
    } finally {
      this.#reconnecting = false;
    }
  }

  /**
   * Redial now, outside the backoff schedule. The schedule is finite: once it is
   * exhausted the stream stays down with the disconnect as its answer, and this
   * is the entry point a caller uses to try again (a reconnect affordance, or a
   * resume-after-sleep watcher such as #832). It resets the attempt count so a
   * fresh try gets the full schedule, and no-ops while closed, unsubscribed, or
   * while a scheduled or in-flight reconnect is already running, so a caller
   * cannot stack redials. A drop the caller deems not worth reconnecting stays
   * down.
   */
  retry(): void {
    if (this.#closed || this.#callbacks === null) return;
    if (this.#cancelReconnect !== null || this.#reconnecting) return;
    if (!this.#active().shouldReconnect()) return;
    this.#backoff.reset();
    void this.#reconnectNow();
  }

  /**
   * Report a disconnect once per outage: the first drop transitions the caller
   * to `disconnected`; the retries that follow, until one recovers, are silent.
   */
  #reportDisconnected(error: Error): void {
    if (this.#disconnectedReported) return;
    this.#disconnectedReported = true;
    this.#emit({status: 'disconnected', error});
  }

  #emit(state: StreamConnectionState): void {
    this.#active().onConnectionState(state);
  }
}
