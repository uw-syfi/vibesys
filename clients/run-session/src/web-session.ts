/**
 * The live session's shell: it runs the connection core (`state/session.ts`) and the run fold
 * (`state/run.ts`) as stores, performs each request through the run connection the composition
 * injects, and turns the browser's lifecycle (visibility, network) into actions. Decisions live in
 * the core; this module only does what the core asks and reports what happened.
 */
import {type CoreState, hasRunEnded, initialCoreState} from '@vibesys/run-state';
import {createStore, type Store} from '@vibesys/store';
import {isSnapshotStoreReplaced, type WebSessionState} from './connection.js';
import type {BrowserLifecycle, RunConnection, RunStreamHandlers, TransportHooks} from './ports.js';
import {type RunAction, runStep} from './run.js';
import {
  type Reachability,
  SESSION_INITIAL,
  type SessionAction,
  type SessionRequest,
  type SessionState,
  type SnapshotResult,
  sessionAcceptsSnapshot,
  sessionStartupSettled,
  sessionStep,
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

/** The run fold's store: what the run page renders as the run. */
export type RunStore = Store<CoreState, RunAction>;

export function createRunStore(): RunStore {
  return createStore(runStep, initialCoreState(), () => undefined);
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

/** The new foreign-run rejection produced by one run-fold transition, if any. */
function runIdentityRejection(previous: CoreState, current: CoreState): Error | null {
  const before = previous.diagnostics.find(item => item.id === 'core-state:run-identity-mismatch');
  const rejected = current.diagnostics.find(item => item.id === 'core-state:run-identity-mismatch');
  return rejected === undefined || rejected === before ? null : new Error(rejected.summary);
}

export class WebSession {
  readonly store: RunStore = createRunStore();
  readonly #core: Store<SessionState, SessionAction>;
  readonly #connection: RunConnection;
  readonly #lifecycle: BrowserLifecycle;
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
    this.#core = createStore(sessionStep, SESSION_INITIAL, request => this.#perform(request));
    this.#connection = connect({
      onConnectionState: state => this.#core.dispatch({type: 'controls', state}),
    });
    this.#lifecycle = lifecycle;
    this.#lifecycle.addEventListener('visibilitychange', this.#wake);
    this.#lifecycle.addEventListener('online', this.#wake);
    this.#lifecycle.addEventListener('offline', this.#offline);
  }

  getState = (): WebSessionState => this.#core.getState().view;

  subscribe = (listener: () => void): (() => void) => this.#core.subscribe(listener);

  /** Establish the stream store, then load its fenced snapshot; settles after both complete. */
  start(): Promise<void> {
    if (this.#started === null) {
      this.#started = new Promise(resolve => {
        this.#startupSettled = resolve;
      });
      this.#core.dispatch({type: 'start'});
    }
    return this.#started;
  }

  close(): Promise<void> {
    if (this.#closed === null) this.#core.dispatch({type: 'close'});
    return this.#closed ?? Promise.resolve();
  }

  /** Reload the run and restart the stream from a fresh bootstrap (the Reattach button). */
  reattach(): void {
    this.#core.dispatch({type: 'reattach', reach: this.#reach(), runEnded: this.#runEnded()});
  }

  /** Redial the control channel now (the Reconnect button); the event stream is untouched. */
  reconnectControls(): void {
    this.#core.dispatch({type: 'reconnect-controls'});
  }

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

  #wake = (): void => this.#core.dispatch({type: 'wake', reach: this.#reach()});

  #offline = (): void => this.#core.dispatch({type: 'offline', runEnded: this.#runEnded()});

  /** Fold one still-owned snapshot and translate projection rejection into a failed load. */
  #foldSnapshot(
    slot: 'refresh' | 'replacement',
    token: number,
    result: SnapshotResult,
  ): SnapshotResult {
    if (!result.ok || !sessionAcceptsSnapshot(this.#core.getState(), slot, token)) return result;
    const previous = this.store.getState();
    this.store.dispatch({type: 'snapshot', snapshot: result.snapshot});
    const rejection = runIdentityRejection(previous, this.store.getState());
    if (rejection !== null) return {ok: false, error: rejection};
    // A snapshot carries a journal position but no events, so it must not advance the event
    // cursor. Within one stream-store generation the evidence is monotone: core-state can reject a
    // stale same-run snapshot's liveness, and that response must not erase a newer known journal
    // position.
    const {snapshot} = result;
    const current = this.#snapshotWatermark;
    this.#snapshotWatermark = {
      runId: snapshot.run_id,
      sequence:
        current?.runId === snapshot.run_id
          ? Math.max(current.sequence, snapshot.sequence)
          : snapshot.sequence,
    };
    return result;
  }

  #foldBatch: RunStreamHandlers['onBatch'] = (message, reconciliation, context): void => {
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
    this.store.dispatch({type: 'batch', message, reconciliation});
    const rejection = runIdentityRejection(previousRun, this.store.getState());
    this.#streamStoreId = storeId;
    if (storeReplaced) {
      // Store identity, not connection or chunk identity, proves a replacement. Retire old
      // evidence, then query through the server's atomic store precondition.
      this.#snapshotWatermark = null;
    }
    if (firstGeneration || requestedFreshGeneration || storeReplaced) {
      this.#core.dispatch({type: 'stream-established', storeId});
    }
    if (requestedFreshGeneration) this.#freshBoundaryAfterConnection = undefined;
    this.#lastBatchConnection = context.connectionGeneration;
    if (rejection !== null) {
      this.#core.dispatch({type: 'projection-rejected', error: rejection});
    }
  };

  #recoverStream(request: Extract<SessionRequest, {kind: 'recover-stream'}>): void {
    try {
      if (request.mode === 'rebootstrap') {
        const disposition = this.#connection.rebootstrap();
        if (disposition === 'transitioning') {
          this.#freshBoundaryAfterConnection = this.#lastBatchConnection;
        } else if (disposition === 'current' && this.#streamStoreId !== null) {
          // The fresh generation delivered its first batch before its handle
          // settled. It is already the stream owner, so retry its fenced
          // snapshot instead of waiting for a generation change that will not
          // occur.
          this.#core.dispatch({type: 'stream-established', storeId: this.#streamStoreId});
        }
      } else {
        this.#connection.retry();
      }
    } catch (error) {
      this.#core.dispatch({type: 'fault', error: toError(error)});
    }
  }

  #perform(request: SessionRequest): void {
    const dispatch = this.#core.dispatch;
    switch (request.kind) {
      case 'subscribe': {
        const {token} = request;
        void this.#connection
          .subscribe({
            cursor: () => this.store.getState().sequence,
            shouldReconnect: () => this.#streamNeedsRecovery(),
            onBatch: this.#foldBatch,
            onFault: error => dispatch({type: 'fault', error}),
            onConnectionState: state => dispatch({type: 'stream', state}),
          })
          .then(
            () => null,
            (error: unknown) => toError(error),
          )
          .then(error => {
            dispatch({type: 'subscribe-reply', token, error});
            this.#settleStartup();
          });
        return;
      }
      case 'refresh': {
        const {token, mode} = request;
        // A browser wake is stronger evidence than either reconnect timer: a background tab may
        // have had both coalesced indefinitely. The redial is inert on a healthy or dialing
        // channel, and otherwise dials before the snapshot request is queued.
        try {
          this.#connection.redialControls();
        } catch (error) {
          dispatch({
            type: 'refresh-reply',
            token,
            outcome: {kind: 'redial-failed', error: toError(error)},
          });
          return;
        }
        if (mode === 'rebootstrap') {
          void Promise.resolve().then(() =>
            dispatch({type: 'refresh-reply', token, outcome: {kind: 'ready'}}),
          );
        } else {
          void this.#loadSnapshot(this.#streamStoreId ?? undefined).then(result => {
            dispatch({
              type: 'refresh-reply',
              token,
              outcome: {kind: 'snapshot', result: this.#foldSnapshot('refresh', token, result)},
            });
          });
        }
        return;
      }
      case 'load-replacement-snapshot': {
        const {token, storeId} = request;
        void this.#loadSnapshot(storeId).then(result => {
          dispatch({
            type: 'replacement-snapshot-reply',
            token,
            storeId,
            result: this.#foldSnapshot('replacement', token, result),
          });
          this.#settleStartup();
        });
        return;
      }
      case 'recover-stream':
        this.#recoverStream(request);
        return;
      case 'reevaluate-stream':
        if (this.#snapshotIsAhead()) this.#connection.retryIfDisconnected();
        return;
      case 'redial-controls':
        this.#connection.redialControls();
        return;
      case 'close':
        this.#lifecycle.removeEventListener('visibilitychange', this.#wake);
        this.#lifecycle.removeEventListener('online', this.#wake);
        this.#lifecycle.removeEventListener('offline', this.#offline);
        this.#closed = this.#connection.close();
        this.#startupSettled();
        return;
    }
  }

  #settleStartup(): void {
    if (sessionStartupSettled(this.#core.getState())) this.#startupSettled();
  }

  /** Load the snapshot; the core decides whether the run fold applies it. */
  async #loadSnapshot(storeId?: string): Promise<SnapshotResult> {
    try {
      return {ok: true, snapshot: await this.#connection.loadSnapshot(storeId)};
    } catch (error) {
      if (isSnapshotStoreReplaced(error)) {
        return {ok: false, error, reason: 'store-replaced'};
      }
      return {ok: false, error: toError(error)};
    }
  }
}
