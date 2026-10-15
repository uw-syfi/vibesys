import type {ServerMessage} from '@vibesys/contracts';
import {BackendClientError, isServerRejection} from '@vibesys/contracts';
import {
  ActiveExecutionCheckpointAssembler,
  type ActiveExecutionCheckpointDisposition,
  activeExecutionCheckpointDisposition,
} from './active-execution-checkpoint.js';
import {BackoffSchedule, DEFAULT_RECONNECT_DELAYS_MS} from './backoff.js';
import {defaultScheduleTimeout, type ScheduleTimeout} from './control-channel.js';
import type {EventSubscription, SubscribeOptions} from './transport.js';

/**
 * The slice of `ServerClient` a persistent stream drives. Both the production
 * client and any transport the caller wraps around it (a tui `ServerTransport`,
 * a test fake) satisfy this structurally, so the abstraction lives here in
 * `@vibesys/transport` without depending on anything above it.
 */
export interface StreamTransport {
  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<EventSubscription>;
}

/** Messages delivered after transport-only checkpoint fragments are assembled. */
export type PersistentStreamMessage = Exclude<ServerMessage, {type: 'active_executions_chunk'}>;

/** Whether the stream currently holds a live subscription. */
export type StreamConnectionState =
  | {readonly status: 'connected'}
  | {readonly status: 'disconnected'; readonly error: Error};

/** Where one accepted stream message was delivered. */
export interface StreamMessageContext {
  /** Whether this dial resumed from the caller's cursor instead of bootstrapping a fresh tail. */
  readonly resumed: boolean;
  /** Monotone identity of the owned dial, used to distinguish in-loop from replacement batches. */
  readonly connectionGeneration: number;
  /** Execution-checkpoint transaction metadata derived by this persistent stream. */
  readonly activeExecutionCheckpoint?: ActiveExecutionCheckpointDisposition;
}

/** What an explicit fresh-bootstrap request means for the caller's current stream generation. */
export type StreamRebootstrapDisposition =
  /** A fresh generation is pending and has not delivered its first batch yet. */
  | 'transitioning'
  /** The coalesced fresh generation has already delivered a batch. */
  | 'current'
  /** The stream is closed or has not been subscribed. */
  | 'inactive';

type StreamRecovery = 'idle' | 'initial' | 'bootstrap' | 'retry' | 'rebootstrap';
type ActiveStreamRecovery = Exclude<StreamRecovery, 'idle'>;
type QueuedStreamRecovery = Extract<StreamRecovery, 'retry' | 'rebootstrap'>;

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
   * Whether a dropped stream is worth redialing. The caller decides this from
   * its domain state, including whether a finished run is known to have events
   * beyond the cursor. When it has complete terminal history, the stream stays
   * down rather than dialing a peer that will never send another event.
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
   * Every semantic stream message, after transport-only checkpoint fragments
   * are assembled, tagged with whether it came from a resume (from the caller's
   * cursor) or a bootstrap (a fresh tail).
   * The dial normally decides the flag, including a batch delivered
   * synchronously before `subscribe` resolves. An in-loop server rebootstrap
   * explicitly overrides a resumed dial for that batch, because it supersedes
   * the cursor even though its socket never disconnected.
   */
  onMessage(message: PersistentStreamMessage, context: StreamMessageContext): void;
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

interface SubscriptionRetirement {
  /** The handle's own close promise, preserving its established scheduling. */
  readonly closing: Promise<void>;
  /** The same teardown with an already-fenced handle's failure contained. */
  readonly settled: Promise<void>;
}

function toError(value: unknown): Error {
  return value instanceof Error ? value : new Error(String(value));
}

/** A frame the client could not interpret, independent of socket lifecycle. */
function isProtocolFault(error: Error): boolean {
  return error instanceof BackendClientError && error.kind === 'parse';
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
  readonly #activeExecutionCheckpoints = new ActiveExecutionCheckpointAssembler();

  #callbacks: PersistentEventStreamCallbacks | null = null;
  #subscription: EventSubscription | null = null;
  /** Close work for a fenced handle that still belongs to this stream. */
  #retiring: Promise<void> | null = null;
  /** Subscribe work that owns any handle the transport has not returned yet. */
  #pendingDial: Promise<void> | null = null;
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
  /**
   * The close that left the stream down, including an ordinary terminal close
   * the caller initially considered harmless. Policy can change after that
   * close (for example, a concurrent snapshot can prove the fold incomplete),
   * so `retry` must be able to promote it into a reported recovery without
   * redialing a stream that is still healthy.
   */
  #disconnectError: Error | null = null;
  /** The one reconnect operation that currently owns the stream's dial slot. */
  #recovery: StreamRecovery = 'idle';
  /**
   * At most one recovery intent retained behind the active dial. A fresh
   * bootstrap dominates a cursor retry because it can satisfy both requests.
   */
  #queuedRecovery: QueuedStreamRecovery | null = null;
  /** Whether the current dial owner has already reported that its connection failed. */
  #recoveryFailed = false;

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
    // The initial bootstrap owns the same dial slot as every later recovery.
    // A wake or explicit reattach can arrive while its handshake is pending;
    // both are already satisfied by this sequence-zero attempt and must not
    // create a second socket whose handle can race the first one home.
    this.#recovery = 'initial';
    this.#recoveryFailed = false;
    let recovered = false;
    try {
      recovered = await this.#bootstrapDial();
    } finally {
      this.#recovery = 'idle';
      // The sequence-zero connection already satisfies every request that
      // arrived behind it. Only a failed initial attempt owes the retained
      // intent another pass through the ordinary recovery entry points.
      if (this.#closed || recovered) this.#queuedRecovery = null;
      else this.#drainQueuedRecovery();
    }
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
    this.#activeExecutionCheckpoints.reset();
    this.#queuedRecovery = null;
    if (this.#cancelReconnect !== null) {
      this.#cancelReconnect();
      this.#cancelReconnect = null;
    }
    const pendingDial = this.#pendingDial;
    if (pendingDial !== null) await pendingDial;
    const retiring = this.#retiring;
    if (retiring !== null) await retiring;
    const subscription = this.#subscription;
    this.#subscription = null;
    await subscription?.close();
  }

  /** Serialize owned handle retirement and contain close errors from already-failed sockets. */
  #retire(subscription: EventSubscription | null): SubscriptionRetirement | null {
    if (subscription === null) return null;
    const previous = this.#retiring;
    let settleRetirement: () => void = () => undefined;
    const retirement = new Promise<void>(resolve => {
      settleRetirement = resolve;
    });
    // Publish the owner before invoking the handle. A close implementation may
    // synchronously ask this stream to shut down, which must join this work.
    this.#trackRetirement(retirement);
    let closing: Promise<void>;
    try {
      closing =
        previous === null ? subscription.close() : previous.then(() => subscription.close());
    } catch (error) {
      closing = Promise.reject(error);
    }
    // The handle is fenced before teardown begins, so a close failure is not
    // allowed to mask stream recovery or strand terminal shutdown.
    const settled = closing.catch(() => undefined);
    // Once invocation returned, routine observers can join the actual close
    // promise directly. A synchronously re-entrant observer already captured
    // the placeholder, which is settled from the same close below.
    if (this.#retiring === retirement) this.#trackRetirement(settled);
    void settled.then(settleRetirement);
    return {closing, settled};
  }

  /** Publish already-started teardown before any caller can race terminal close. */
  #trackRetirement(retiring: Promise<void>): Promise<void> {
    this.#retiring = retiring;
    void retiring.then(() => {
      if (this.#retiring === retiring) this.#retiring = null;
    });
    return retiring;
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
        if (this.#recoverySuperseded()) return false;
        if (!isServerRejection(error)) {
          this.#recoveryFailed = true;
          this.#reportDisconnected(toError(error));
          return false;
        }
        // A protocol_error is delivered before the typed rejection that ends
        // a capability probe. It fences that one dial, but the rejection is
        // the expected evidence that authorizes this fallback. Do not let the
        // retired probe poison the replacement attempt owned by this recovery.
        this.#recoveryFailed = false;
        // The server refused `tail`; fall through to a full replay.
      }
    }
    try {
      return await this.#dial(0, false);
    } catch (error) {
      if (this.#closed) return false;
      this.#recoveryFailed = true;
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
   * schedule retries with the store intact. A failed attempt reports the
   * disconnect once. Usually the triggering drop already did that, but a
   * lifecycle retry may proactively retire a live stream, whose failed
   * replacement must not leave the caller claiming it is connected.
   */
  async #resumeDial(): Promise<boolean> {
    const cursor = this.#active().cursor();
    const storeId = this.#active().storeId();
    if (storeId) {
      try {
        return await this.#dial(cursor, true, {storeId});
      } catch (error) {
        if (this.#recoverySuperseded()) return false;
        if (!isServerRejection(error)) {
          this.#recoveryFailed = true;
          this.#reportDisconnected(toError(error));
          return false;
        }
        // The rejected named dial is retired. Its protocol_error belongs to
        // the capability probe, not to the cursor-only fallback below.
        this.#recoveryFailed = false;
        // The server refused `store_id`; fall through to a cursor-only resume.
      }
    }
    try {
      return await this.#dial(cursor, true);
    } catch (error) {
      if (!this.#recoverySuperseded()) {
        this.#recoveryFailed = true;
        this.#reportDisconnected(toError(error));
      }
      return false;
    }
  }

  /**
   * One subscribe attempt. Binds the resume flag and the connection identity
   * into the message and disconnect handlers before dialing, so a batch the
   * server delivers inside `subscribe` (before the promise resolves) is tagged
   * and attributed correctly. A returned handle is adopted only while its
   * token is still current; a superseded handle closes itself instead of
   * replacing the subscription that superseded it. A later
   * `event_batch.rebootstrap` is the one server-declared exception: it is fresh
   * despite this dial having resumed.
   */
  async #dial(
    afterSequence: number,
    resumed: boolean,
    options?: SubscribeOptions,
  ): Promise<boolean> {
    const token = ++this.#connectionSeq;
    this.#activeExecutionCheckpoints.reset();
    let settleDial: () => void = () => undefined;
    const pendingDial = new Promise<void>(resolve => {
      settleDial = resolve;
    });
    // Publish ownership before invoking the transport: both real transports
    // can deliver a protocol fault synchronously inside subscribe(), before
    // they return the promise that eventually yields this handle.
    this.#pendingDial = pendingDial;
    try {
      const subscription = await this.#transport.subscribe(
        afterSequence,
        message => this.#deliver(message, resumed, token),
        error => this.#handleDisconnect(error, token),
        options,
      );
      // A transport may report this socket's disconnect before its subscribe
      // promise hands the handle back. Such a handle is already dead and cannot
      // become authoritative merely because its promise resolves afterward.
      if (this.#closed || token !== this.#connectionSeq || this.#recoveryFailed) {
        const retired = this.#retire(subscription);
        if (retired !== null) await retired.settled;
        return false;
      }
      this.#subscription = subscription;
      return true;
    } finally {
      settleDial();
      if (this.#pendingDial === pendingDial) this.#pendingDial = null;
    }
  }

  #deliver(delivered: ServerMessage, resumed: boolean, token: number): void {
    if (this.#closed || token !== this.#connectionSeq) return;
    const message = this.#activeExecutionCheckpoints.accept(delivered);
    if (message === null) return;
    // A protocol error is the terminal frame for this stream generation.
    // Fence it before calling outward so neither another synchronous frame nor
    // a reentrant recovery can mistake this socket for a current generation.
    // A pre-subscribe capability rejection is handled by the enclosing dial,
    // which explicitly clears the recovery-local failure before its fallback.
    if (message.type === 'protocol_error') {
      if (this.#recovery !== 'idle') this.#recoveryFailed = true;
      this.#connectionSeq += 1;
      const failed = this.#subscription;
      this.#subscription = null;
      this.#retire(failed);
    }
    const batchRebootstrap = message.type === 'event_batch' && message.rebootstrap === true;
    const messageResumed = resumed && !batchRebootstrap;
    if (!messageResumed && message.type === 'event_batch') this.#bootstrapped = true;
    const activeExecutionCheckpoint =
      message.type === 'event_batch' ? activeExecutionCheckpointDisposition(message) : undefined;
    this.#active().onMessage(message, {
      resumed: messageResumed,
      connectionGeneration: token,
      ...(activeExecutionCheckpoint === undefined ? {} : {activeExecutionCheckpoint}),
    });
  }

  /**
   * A live subscription dropped: decide and own any retry before reporting the
   * completed transition to external code.
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
   * After the bootstrap, a transport drop the caller declines to redial is the
   * socket closing behind history the caller already has, with nothing more
   * coming: silent, as before. A parse failure is different evidence. It says
   * the peer sent a frame this client cannot consume, so it is always reported
   * even when run lifecycle declines an automatic redial. The caller can then
   * offer the explicit fresh-bootstrap recovery below without turning every
   * clean terminal close into an outage.
   */
  #handleDisconnect(error: Error, token: number): void {
    // A stale subscription's late close is not this stream's outage.
    if (this.#closed || token !== this.#connectionSeq) return;
    this.#activeExecutionCheckpoints.reset();
    if (this.#recovery !== 'idle') this.#recoveryFailed = true;
    this.#disconnectError = error;
    const redialing = this.#active().shouldReconnect();
    // Arm the retry before the external state callback. A callback may request
    // an explicit re-bootstrap synchronously; that stronger recovery must see
    // and cancel this timer rather than leave it to replace the fresh stream.
    if (redialing) this.#scheduleReconnect();
    if (isProtocolFault(error) || redialing || !this.#bootstrapped) {
      this.#reportDisconnected(error);
    }
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

  async #reconnectNow(rebootstrap = false): Promise<void> {
    if (this.#recovery !== 'idle') return;
    if (!this.#canStartRecovery(rebootstrap)) return;
    const recovery = this.#recoveryMode(rebootstrap);
    this.#recovery = recovery;
    this.#recoveryFailed = false;
    // The old handle is fenced below, so its bootstrap is no longer the
    // caller's current generation. Clear this before awaiting its close:
    // another explicit request may coalesce while close is still pending and
    // must see that the replacement has not delivered a batch yet.
    if (recovery === 'rebootstrap') this.#bootstrapped = false;
    // The recovery owns delivery now, before closing the old handle. A close
    // may settle asynchronously, and no message from the retired token may
    // enter the fold while the next dial waits for it.
    this.#connectionSeq += 1;
    try {
      const stale = this.#subscription;
      this.#subscription = null;
      // A protocol fault may already have detached and begun closing a handle.
      // Do not dial its replacement until every owned close retires.
      const retiring = this.#retiring;
      if (retiring !== null) await retiring;
      const staleRetirement = this.#retire(stale);
      if (staleRetirement !== null) {
        try {
          await staleRetirement.closing;
        } catch {
          // The subscription is already fenced; closing it owes nothing.
        }
      }
      // An explicit fresh intent can arrive while the cursor retry is still
      // retiring its old handle. Skip a dial it has already superseded.
      if (this.#recoverySuperseded()) return;
      const recovered =
        recovery === 'retry' ? await this.#resumeDial() : await this.#bootstrapDial();
      // The cursor attempt has settled, but it is not the requested terminal
      // recovery anymore. Its handle is retired by the queued fresh pass; do
      // not briefly report recovery or arm another retry in between.
      if (this.#recoverySuperseded()) return;
      if (recovered) {
        this.#markRecovered();
      } else if (recovery !== 'rebootstrap' || this.#active().shouldReconnect()) {
        this.#scheduleReconnect();
      }
    } finally {
      this.#recovery = 'idle';
      this.#drainQueuedRecovery();
    }
  }

  #markRecovered(): void {
    this.#backoff.reset();
    if (this.#queuedRecovery === 'retry') return;
    this.#disconnectError = null;
    this.#disconnectedReported = false;
    this.#emit({status: 'connected'});
  }

  #canStartRecovery(rebootstrap: boolean): boolean {
    return !this.#closed && (rebootstrap || this.#active().shouldReconnect());
  }

  #recoveryMode(rebootstrap: boolean): ActiveStreamRecovery {
    if (rebootstrap) return 'rebootstrap';
    return this.#bootstrapped ? 'retry' : 'bootstrap';
  }

  /** Whether teardown or a stronger queued intent superseded this recovery. */
  #recoverySuperseded(): boolean {
    return this.#closed || (this.#recovery !== 'initial' && this.#queuedRecovery === 'rebootstrap');
  }

  /** Retain one intent, without letting a later cursor retry weaken a fresh request. */
  #queueRecovery(recovery: QueuedStreamRecovery): boolean {
    if (this.#queuedRecovery === 'rebootstrap' || this.#queuedRecovery === recovery) return false;
    this.#queuedRecovery = recovery;
    return true;
  }

  /** Release the dial slot before replaying its one retained intent through the public policy. */
  #drainQueuedRecovery(): void {
    const queued = this.#queuedRecovery;
    this.#queuedRecovery = null;
    if (queued === 'rebootstrap') this.rebootstrap();
    else if (queued === 'retry') this.retry();
  }

  /** Classify a fresh request that overlaps the sequence-zero bootstrap. */
  #initialRebootstrapDisposition(): StreamRebootstrapDisposition {
    // A healthy bootstrap batch establishes the fresh generation before some
    // transports return its subscription handle. The caller can already fence
    // and validate that generation, so report it as current rather than retain
    // an intent that initial success would discard. A disconnect after that
    // batch but before the handle settles has already killed the generation;
    // preserve the explicit recovery in that composition just as for any
    // other failed initial attempt.
    if (this.#bootstrapped && !this.#recoveryFailed) return 'current';
    this.#queueRecovery('rebootstrap');
    return 'transitioning';
  }

  /**
   * Reload the stream from a fresh bootstrap, whatever automatic-redial policy
   * currently says. This is the explicit recovery for a reported protocol
   * fault on an ended run: run lifecycle still keeps ordinary closes quiet and
   * prevents automatic dialing, while an operator can ask the peer for one new
   * complete view. The current subscription and pending timer are retired so
   * the recovery owns exactly one fresh dial; a live run retains its normal
   * schedule if that dial fails. A cursor dial already in flight cannot be
   * cancelled through the transport contract, so one fresh intent is queued:
   * its returned handle is retired before the sequence-zero dial begins.
   */
  rebootstrap(): StreamRebootstrapDisposition {
    if (this.#closed || this.#callbacks === null) return 'inactive';
    if (this.#recovery !== 'idle') {
      if (this.#recovery === 'initial') return this.#initialRebootstrapDisposition();
      // A fresh bootstrap is stronger than a cursor retry. It is also a new
      // attempt once the active fresh dial has already reported failure, even
      // if that dial's subscribe promise has not retired its handle yet.
      const failedFreshRecovery = this.#recovery === 'rebootstrap' && this.#recoveryFailed;
      if (
        (this.#recovery === 'retry' || failedFreshRecovery) &&
        this.#queueRecovery('rebootstrap')
      ) {
        // The pending cursor dial cannot be cancelled through StreamTransport,
        // but its callbacks can be fenced immediately. When its handle arrives,
        // #dial observes the retired token and closes it before the fresh pass.
        this.#connectionSeq += 1;
      }
      // A fresh recovery that has already delivered its first batch is this
      // caller's current generation, even while its subscription handle is
      // still pending. Coalescing another request must not make consumers
      // retire state that belongs to that already-observed generation.
      return this.#queuedRecovery !== 'rebootstrap' &&
        this.#recovery !== 'retry' &&
        this.#bootstrapped
        ? 'current'
        : 'transitioning';
    }
    if (this.#cancelReconnect !== null) {
      this.#cancelReconnect();
      this.#cancelReconnect = null;
    }
    this.#backoff.reset();
    void this.#reconnectNow(true);
    return 'transitioning';
  }

  /**
   * Redial now, outside the backoff schedule. The schedule is finite: once it is
   * exhausted the stream stays down with the disconnect as its answer, and this
   * is the entry point a caller uses to try again (a reconnect affordance, or a
   * resume-after-sleep watcher such as #832). It resets the attempt count so a
   * fresh try gets the full schedule. An armed timer is cancelled because a
   * browser wake is evidence that waiting for a timer coalesced while the page
   * slept is no longer useful; an actual dial already in flight still owns the
   * attempt, so a caller cannot stack redials. Closed, unsubscribed, and ended
   * streams stay down.
   */
  retry(): void {
    if (this.#closed || this.#callbacks === null) return;
    if (this.#recovery !== 'idle') {
      if (this.#recovery === 'initial') {
        if (this.#active().shouldReconnect()) this.#queueRecovery('retry');
        return;
      }
      // A snapshot can prove the new stream incomplete after its terminal
      // socket has already closed, while the fresh dial still owns this slot.
      // Retain one weaker intent so finally reevaluates shouldReconnect after
      // that recovery is fully adopted instead of dropping the only wake-up.
      // An ordinary cursor recovery already is that retry, and repeated wake
      // notifications must continue to coalesce with it.
      if (
        this.#recovery === 'rebootstrap' &&
        this.#disconnectError !== null &&
        this.#active().shouldReconnect()
      ) {
        this.#queueRecovery('retry');
      }
      return;
    }
    const disconnectError = this.#disconnectError;
    if (!this.#active().shouldReconnect()) return;
    if (disconnectError !== null) {
      // The original close may have been quiet because the caller had no
      // reason to recover then. New evidence made it actionable, so report the
      // outage before redialing just like an immediately recoverable close.
      this.#reportDisconnected(disconnectError);
    }
    if (this.#cancelReconnect !== null) {
      this.#cancelReconnect();
      this.#cancelReconnect = null;
    }
    this.#backoff.reset();
    void this.#reconnectNow();
  }

  /**
   * Reconsider a stream that may have closed while caller policy declined a
   * redial. Unlike `retry`, this is inert while the current connection remains
   * healthy, so an asynchronously learned recovery condition cannot replace a
   * live subscription merely to discover whether it had dropped.
   */
  retryIfDisconnected(): void {
    if (this.#disconnectError === null) return;
    this.retry();
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
