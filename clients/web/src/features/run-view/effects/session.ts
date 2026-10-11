/**
 * The live session's shell: it runs the connection core (`state/session.ts`) and the run fold
 * (`entities/run.ts`) as stores, performs each request through the run connection the composition
 * injects, and turns the browser's lifecycle (visibility, network) into actions. Decisions live in
 * the core; this module only does what the core asks and reports what happened.
 */
import {
  type CoreState,
  createStore,
  hasRunEnded,
  initialCoreState,
  type Store,
} from '@vibesys/core-state';
import type {WebSessionState} from '../../../entities/connection.js';
import {type RunAction, runStep} from '../../../entities/run.js';
import type {RunConnection, TransportHooks} from '../../../platform/backend.js';
import type {BrowserLifecycle} from '../../../platform/lifecycle.js';
import {
  type Reachability,
  SESSION_INITIAL,
  type SessionAction,
  type SessionRequest,
  type SessionState,
  type SnapshotResult,
  sessionStep,
} from '../state/session.js';

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

export class WebSession {
  readonly store: RunStore = createRunStore();
  readonly #core: Store<SessionState, SessionAction>;
  readonly #connection: RunConnection;
  readonly #lifecycle: BrowserLifecycle;
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

  /** Load the snapshot, then subscribe to the stream; settles once the first subscription does. */
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

  #wake = (): void => this.#core.dispatch({type: 'wake', reach: this.#reach()});

  #offline = (): void => this.#core.dispatch({type: 'offline', runEnded: this.#runEnded()});

  #perform(request: SessionRequest): void {
    const dispatch = this.#core.dispatch;
    switch (request.kind) {
      case 'load-snapshot': {
        const {token} = request;
        void this.#loadSnapshot().then(result => dispatch({type: 'snapshot-reply', token, result}));
        return;
      }
      case 'apply-snapshot': {
        this.store.dispatch({type: 'snapshot', snapshot: request.snapshot});
        return;
      }
      case 'subscribe': {
        const {token} = request;
        void this.#connection
          .subscribe({
            cursor: () => this.store.getState().sequence,
            shouldReconnect: () => !this.#runEnded(),
            onBatch: (message, reconciliation) =>
              this.store.dispatch({type: 'batch', message, reconciliation}),
            onFault: error => dispatch({type: 'fault', error}),
            onConnectionState: state => dispatch({type: 'stream', state}),
          })
          .then(
            () => null,
            (error: unknown) => toError(error),
          )
          .then(error => {
            dispatch({type: 'subscribe-reply', token, error});
            this.#startupSettled();
          });
        return;
      }
      case 'refresh': {
        const {token} = request;
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
        void this.#loadSnapshot().then(result =>
          dispatch({type: 'refresh-reply', token, outcome: {kind: 'snapshot', result}}),
        );
        return;
      }
      case 'recover-stream':
        try {
          if (request.mode === 'rebootstrap') this.#connection.rebootstrap();
          else this.#connection.retry();
        } catch (error) {
          dispatch({type: 'fault', error: toError(error)});
        }
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

  /** Load the snapshot; the core decides whether the run fold applies it. */
  async #loadSnapshot(): Promise<SnapshotResult> {
    try {
      return {ok: true, snapshot: await this.#connection.loadSnapshot()};
    } catch (error) {
      return {ok: false, error: toError(error)};
    }
  }
}
