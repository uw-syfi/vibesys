/**
 * The live session's shell: it runs the session machine (`session.ts`) and the run fold
 * (`run.ts`), provides the machine's operations and actions through the run connection the
 * composition injects, and turns the browser's lifecycle (visibility, network) into events.
 * Decisions live in the machine; this module only does what the machine asks and reports what
 * happened. A connection method that throws is reported to the machine as a stream `fault`; an
 * action that throws anyway (a run-store subscriber failing while a snapshot folds) stops the
 * machine, and the shell then releases the connection as a close would.
 *
 * A snapshot load reports success only for a snapshot the run fold would accept: the operation
 * dry-runs the fold and reports a foreign run as a failed load, so it never proves the page
 * healthy. The machine still folds it later, in its `applySnapshot` action, which keeps the fold
 * ordered by the machine's issue order. A rejection the dry-run could not foresee (the run's
 * identity latched between the operation settling and the machine taking its output) and a
 * rejected stream batch are reported as `projection-rejected`.
 */
import type {RunSnapshot, TuiDefaults} from '@vibesys/contracts';
import {
  type CoreState,
  hasRunEnded,
  type IterationNavigationState,
  initialIterationNavigation,
  reduceIterationResult,
} from '@vibesys/run-state';
import type {AbortSignalLike} from '@vibesys/transport';
import {type Actor, createActor, fromPromise} from 'xstate';
import {
  isIterationStoreReplaced,
  isSnapshotStoreReplaced,
  type WebSessionState,
} from './connection.js';
import type {
  BrowserLifecycle,
  IterationLoad,
  RunCommand,
  RunCommandOutcome,
  RunConnection,
  RunStreamHandlers,
  TransportHooks,
} from './ports.js';
import {
  createRunStore,
  type IterationPublicationOutcome,
  type RunAction,
  type RunStore,
  runStep,
} from './run.js';
import {
  type IterationQueryOutcome,
  type Reachability,
  type RefreshOutcome,
  type SessionEvent,
  type SessionSnapshot,
  type SnapshotResult,
  type StreamRecovery,
  sessionMachine,
  sessionStartupSettled,
  toError,
} from './session.js';

/** What the composition injects: the run's connection and the page's lifecycle. */
export interface WebSessionOptions {
  /**
   * Open the run's connection, wired to report control-channel connectivity to `hooks`. A factory
   * rather than an instance because the session, not the caller, owns what the transport reports
   * to.
   */
  readonly connect: (hooks: TransportHooks) => RunConnection;
  readonly lifecycle: BrowserLifecycle;
}

/** The new foreign-run rejection produced by one run-fold transition, if any. */
function runIdentityRejection(previous: CoreState, current: CoreState): Error | null {
  const before = previous.diagnostics.find(item => item.id === 'core-state:run-identity-mismatch');
  const rejected = current.diagnostics.find(item => item.id === 'core-state:run-identity-mismatch');
  return rejected === undefined || rejected === before ? null : new Error(rejected.summary);
}

export class WebSession {
  readonly store: RunStore = createRunStore();
  readonly #actor: Actor<typeof sessionMachine>;
  readonly #connection: RunConnection;
  readonly #lifecycle: BrowserLifecycle;
  readonly #listeners = new Set<() => void>();
  /** The machine's newest snapshot: listeners hear only when it changes. */
  #snapshot: SessionSnapshot;
  /** The latest accepted control snapshot, which may prove the event fold is incomplete. */
  #snapshotWatermark: {readonly runId: string; readonly sequence: number} | null = null;
  /** The event store whose batches currently own the run fold. */
  #streamStoreId: string | null = null;
  /** Last dial generation from which a batch entered the fold. */
  #lastBatchConnection: number | null = null;
  /** Active fresh-bootstrap boundary, absent when no recovery owns one. */
  #freshBoundaryAfterConnection: number | null | undefined;
  #started: Promise<void> | null = null;
  #startupSettled: () => void = () => undefined;
  #closed: Promise<void> | null = null;

  constructor({connect, lifecycle}: WebSessionOptions) {
    this.#lifecycle = lifecycle;
    // The machine runs before the connection opens, so a report made while connecting has a
    // machine to reach, and an action that throws always has an observer that releases.
    this.#actor = createActor(this.#provided());
    this.#snapshot = this.#actor.getSnapshot();
    this.#actor.subscribe({
      next: snapshot => this.#observe(snapshot),
      error: () => this.#release(),
    });
    this.#actor.start();
    this.#connection = connect({
      onConnectionState: state => this.#send({type: 'controls', state}),
    });
    this.#lifecycle.addEventListener('visibilitychange', this.#wake);
    this.#lifecycle.addEventListener('online', this.#wake);
    this.#lifecycle.addEventListener('offline', this.#offline);
  }

  getState = (): WebSessionState => this.#snapshot.context.view;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  /** Establish the stream store, then load its fenced snapshot; settles after both complete. */
  start(): Promise<void> {
    if (this.#started === null) {
      this.#started = new Promise(resolve => {
        this.#startupSettled = resolve;
      });
      if (this.#closed === null) this.#send({type: 'start'});
      else this.#startupSettled();
    }
    return this.#started;
  }

  /** Stop the machine and release the connection, also when the machine has already stopped. */
  close(): Promise<void> {
    this.#send({type: 'close'});
    return this.#release();
  }

  /** Reload the run and restart the stream from a fresh bootstrap (the Reattach button). */
  reattach(): void {
    this.#send({type: 'reattach', reach: this.#reach(), runEnded: this.#runEnded()});
  }

  /** Redial the control channel now (the Reconnect button); the event stream is untouched. */
  reconnectControls(): void {
    this.#send({type: 'reconnect-controls'});
  }

  /** Send to the machine while it runs; a closed or failed session drops every later report. */
  #send(event: SessionEvent): void {
    if (this.#actor.getSnapshot().status === 'active') this.#actor.send(event);
  }

  /** XState notifies on every event, also one that changed nothing: publish only changes. */
  #observe(snapshot: SessionSnapshot): void {
    if (snapshot === this.#snapshot) return;
    this.#snapshot = snapshot;
    if (sessionStartupSettled(snapshot)) this.#startupSettled();
    for (const listener of this.#listeners) listener();
  }

  /** Remove the page's listeners and close the connection, once, on every exit path. */
  #release(): Promise<void> {
    if (this.#closed === null) {
      this.#lifecycle.removeEventListener('visibilitychange', this.#wake);
      this.#lifecycle.removeEventListener('online', this.#wake);
      this.#lifecycle.removeEventListener('offline', this.#offline);
      this.#closed = this.#connection.close();
      this.#startupSettled();
    }
    return this.#closed;
  }

  /** Send one operator command without letting presentation code own the transport. */
  command = (input: RunCommand): Promise<RunCommandOutcome> => this.#connection.command(input);

  /** Read the server-selected presentation defaults through the session-owned connection. */
  tuiDefaults = (): Promise<TuiDefaults | null> => this.#connection.tuiDefaults();

  #reach(): Reachability {
    return {visible: this.#lifecycle.visibilityState !== 'hidden', online: this.#lifecycle.online};
  }

  #runEnded(): boolean {
    return hasRunEnded(this.store.getState());
  }

  #snapshotIsAhead(): boolean {
    const run = this.store.getState();
    const watermark = this.#snapshotWatermark;
    return watermark !== null && watermark.runId === run.runId && run.sequence < watermark.sequence;
  }

  /** Whether the run can still emit, or its accepted snapshot is ahead of the event cursor. */
  #streamNeedsRecovery(): boolean {
    return !this.#runEnded() || this.#snapshotIsAhead();
  }

  #wake = (): void => this.#send({type: 'wake', reach: this.#reach()});

  #offline = (): void => this.#send({type: 'offline', runEnded: this.#runEnded()});

  /** Call the connection; a throw is a fault of the stream it was meant to recover. */
  #guarded(call: () => void): void {
    try {
      call();
    } catch (error) {
      this.#send({type: 'fault', error: toError(error)});
    }
  }

  /** The machine with its operations and actions performed through the run connection. */
  #provided() {
    return sessionMachine.provide({
      actors: {
        subscribe: fromPromise<void, void>(() =>
          this.#connection.subscribe(this.#streamHandlers()),
        ),
        refresh: fromPromise<RefreshOutcome, {readonly mode: StreamRecovery}>(({input, signal}) =>
          this.#refresh(input.mode, signal),
        ),
        loadReplacementSnapshot: fromPromise<SnapshotResult, {readonly storeId: string}>(
          ({input, signal}) => this.#loadSnapshot(signal, input.storeId),
        ),
        loadIterations: fromPromise<IterationQueryOutcome, {readonly storeId: string}>(
          ({input, signal}) => this.#collectIterationPages(signal, input.storeId),
        ),
      },
      actions: {
        applySnapshot: (_, {snapshot}) => {
          if (snapshot !== null) this.#applySnapshot(snapshot);
        },
        recoverStream: (_, {mode}) => this.#guarded(() => this.#recoverStream(mode)),
        // Retry only if the newly applied snapshot is ahead of the event fold.
        reevaluateStream: (_, {applied}) => {
          if (applied && this.#snapshotIsAhead()) {
            this.#guarded(() => this.#connection.retryIfDisconnected());
          }
        },
        redialControls: () => this.#guarded(() => this.#connection.redialControls()),
        publishIterations: (_, {navigation}) => {
          if (navigation !== null) {
            this.#reportIterationMismatch(this.store.publishIterations(navigation));
          }
        },
        closeShell: () => void this.#release(),
      },
    });
  }

  #applySnapshot(snapshot: RunSnapshot): void {
    const previous = this.store.getState();
    this.#dispatchRun({type: 'snapshot', snapshot});
    const rejection = runIdentityRejection(previous, this.store.getState());
    if (rejection !== null) {
      // The load's dry run accepted this snapshot, but the run latched another identity before the
      // machine took the load's output. The machine already trusted it, so report the rejection:
      // the page stays stale until a later fenced snapshot succeeds.
      this.#send({type: 'projection-rejected', error: rejection});
      return;
    }
    // A snapshot carries a journal position but no events, so it must not advance the event
    // cursor. Retain that position only when core-state accepted its run identity. Within one
    // stream-store generation the evidence is monotone: core-state can reject a stale same-run
    // snapshot's liveness, and that response must not erase a newer known journal position.
    const current = this.#snapshotWatermark;
    this.#snapshotWatermark = {
      runId: snapshot.run_id,
      sequence:
        current?.runId === snapshot.run_id
          ? Math.max(current.sequence, snapshot.sequence)
          : snapshot.sequence,
    };
  }

  #recoverStream(mode: StreamRecovery): void {
    if (mode === 'retry') {
      this.#connection.retry();
      return;
    }
    const disposition = this.#connection.rebootstrap();
    if (disposition === 'transitioning') {
      this.#freshBoundaryAfterConnection = this.#lastBatchConnection;
    } else if (disposition === 'current' && this.#streamStoreId !== null) {
      // The fresh generation delivered its first batch before its handle settled. It is already
      // the stream owner, so retry its fenced snapshot instead of waiting for a generation change
      // that will not occur.
      this.#send({type: 'stream-established', storeId: this.#streamStoreId});
      this.#send({type: 'iterations-refresh', storeId: this.#streamStoreId});
    }
  }

  /** Fold one batch and refresh the iteration projection owned by its stream store. */
  #foldBatch(
    ...[message, reconciliation, context]: Parameters<RunStreamHandlers['onBatch']>
  ): void {
    const storeId = message.store_id ?? '';
    const previousStoreId = this.#streamStoreId;
    const boundary = this.#freshBoundaryAfterConnection;
    const requestedFreshGeneration =
      boundary !== undefined &&
      !context.resumed &&
      (boundary === null || context.connectionGeneration > boundary);
    const firstGeneration = previousStoreId === null;
    const storeReplaced = previousStoreId !== null && storeId !== previousStoreId;
    const previousRun = this.store.getState();
    this.#dispatchRun({
      type: 'batch',
      message,
      reconciliation,
      ...(context.activeExecutionCheckpoint === undefined
        ? {}
        : {activeExecutionCheckpoint: context.activeExecutionCheckpoint}),
    });
    const rejection = runIdentityRejection(previousRun, this.store.getState());
    this.#streamStoreId = storeId;
    if (storeReplaced) {
      // Store identity, not connection or chunk identity, proves a replacement. Retire old
      // evidence, then query through the server's atomic store precondition.
      this.#snapshotWatermark = null;
    }
    if (firstGeneration || requestedFreshGeneration || storeReplaced) {
      this.#send({type: 'stream-established', storeId});
    }
    if (requestedFreshGeneration) this.#freshBoundaryAfterConnection = undefined;
    this.#lastBatchConnection = context.connectionGeneration;
    if (rejection !== null) {
      this.#send({type: 'projection-rejected', error: rejection});
      return;
    }
    if (
      firstGeneration ||
      requestedFreshGeneration ||
      storeReplaced ||
      message.events.some(event => event.type === 'iterations_changed')
    ) {
      this.#send({type: 'iterations-refresh', storeId});
    }
  }

  /** What the event stream reports: batches into the run fold, the rest to the machine. */
  #streamHandlers(): RunStreamHandlers {
    return {
      cursor: () => this.store.getState().sequence,
      shouldReconnect: () => this.#streamNeedsRecovery(),
      onBatch: (...args) => this.#foldBatch(...args),
      onFault: error => this.#send({type: 'fault', error}),
      onConnectionState: state => {
        this.#send({type: 'stream', state});
      },
    };
  }

  /**
   * A browser wake is stronger evidence than either reconnect timer: a background tab may have had
   * both coalesced indefinitely. The redial is inert on a healthy or dialing channel, and otherwise
   * dials before the snapshot request is queued. A fresh bootstrap loads no snapshot here: its
   * snapshot is fenced to the store its first batch establishes.
   */
  async #refresh(mode: StreamRecovery, signal: AbortSignalLike): Promise<RefreshOutcome> {
    try {
      this.#connection.redialControls();
    } catch (error) {
      return {kind: 'redial-failed', error: toError(error)};
    }
    if (mode === 'rebootstrap') return {kind: 'ready'};
    return {
      kind: 'snapshot',
      result: await this.#loadSnapshot(signal, this.#streamStoreId ?? undefined),
    };
  }

  /**
   * Load the snapshot; the machine decides whether the run fold applies it. A snapshot the run fold
   * would reject as another run's is a failed load: it must not prove the streamed run healthy.
   * While the load is still owned (its operation not stopped), the rejection is recorded in the
   * fold, which changes only the fold's diagnostics.
   */
  async #loadSnapshot(signal: AbortSignalLike, storeId?: string): Promise<SnapshotResult> {
    let snapshot: RunSnapshot;
    try {
      snapshot = await this.#connection.loadSnapshot(storeId, signal);
    } catch (error) {
      if (isSnapshotStoreReplaced(error)) {
        return {ok: false, error, reason: 'store-replaced'};
      }
      return {ok: false, error: toError(error)};
    }
    const run = this.store.getState();
    const rejection = runIdentityRejection(run, runStep(run, {type: 'snapshot', snapshot}).state);
    if (rejection === null) return {ok: true, snapshot};
    if (!signal.aborted) this.#dispatchRun({type: 'snapshot', snapshot});
    return {ok: false, error: rejection};
  }

  /** Fold run data and report a deferred navigation result once run identity resolves against it. */
  #dispatchRun(action: RunAction): void {
    this.#reportIterationMismatch(this.store.dispatch(action));
  }

  /** Keep iteration projection failures on their independent session-health channel. */
  #reportIterationMismatch(outcome: IterationPublicationOutcome | null): void {
    if (outcome?.kind !== 'run-mismatch') return;
    this.#send({
      type: 'iteration-rejected',
      error: new Error(
        `Ignored iteration navigation for run ${outcome.receivedRunId}; ` +
          `this session owns run ${outcome.expectedRunId}`,
      ),
    });
  }

  async #collectIterationPages(
    signal: AbortSignalLike,
    expectedStoreId: string,
  ): Promise<IterationNavigationState | null> {
    let cursor: string | null = null;
    const seen = new Set<string>();
    let staged = initialIterationNavigation();
    do {
      const loaded = await this.#loadIterationPage(cursor, expectedStoreId, signal);
      if (loaded === null || loaded.kind !== 'result') return null;
      staged = reduceIterationResult(staged, loaded.result);
      if (!('items' in loaded.result)) return staged;
      cursor = loaded.result.next_cursor ?? null;
      if (cursor !== null && seen.has(cursor)) {
        throw new Error('iteration query repeated a paging cursor');
      }
      if (cursor !== null) seen.add(cursor);
    } while (cursor !== null);
    return staged;
  }

  async #loadIterationPage(
    cursor: string | null,
    expectedStoreId: string,
    signal: AbortSignalLike,
  ): Promise<IterationLoad | null> {
    if (signal.aborted) return null;
    try {
      const loaded = await this.#connection.loadIterations(cursor, expectedStoreId, signal);
      // A Fake or an adapter predating request cancellation can still settle after ownership was
      // lost. Fence the continuation here so an abandoned first page never launches page two.
      return signal.aborted ? null : loaded;
    } catch (error) {
      if (isIterationStoreReplaced(error)) return null;
      throw error;
    }
  }
}
