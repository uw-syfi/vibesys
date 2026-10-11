/**
 * The live session's shell: it runs the session machine (`session.ts`) and the run fold
 * (`run.ts`), provides the machine's operations and actions through the run connection the
 * composition injects, and turns the browser's lifecycle (visibility, network) into events.
 * Decisions live in the machine; this module only does what the machine asks and reports what
 * happened.
 */
import {hasRunEnded} from '@vibesys/run-state';
import {type Actor, createActor, fromPromise} from 'xstate';
import type {WebSessionState} from './connection.js';
import type {BrowserLifecycle, RunConnection, TransportHooks} from './ports.js';
import {createRunStore, type RunStore} from './run.js';
import {
  type Reachability,
  type RefreshOutcome,
  type SessionEvent,
  type SnapshotResult,
  sessionMachine,
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

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

export class WebSession {
  readonly store: RunStore = createRunStore();
  readonly #actor: Actor<typeof sessionMachine>;
  readonly #connection: RunConnection;
  readonly #lifecycle: BrowserLifecycle;
  readonly #listeners = new Set<() => void>();
  #view: WebSessionState;
  #started: Promise<void> | null = null;
  #startupSettled: () => void = () => undefined;
  #closed: Promise<void> | null = null;

  constructor({connect, lifecycle}: WebSessionOptions) {
    this.#connection = connect({
      onConnectionState: state => this.#send({type: 'controls', state}),
    });
    this.#lifecycle = lifecycle;
    this.#actor = createActor(this.#provided());
    this.#view = this.#actor.getSnapshot().context.view;
    this.#actor.subscribe(snapshot => {
      if (snapshot.status === 'done' || snapshot.matches({open: {startup: 'done'}})) {
        this.#startupSettled();
      }
      if (snapshot.context.view === this.#view) return;
      this.#view = snapshot.context.view;
      for (const listener of this.#listeners) listener();
    });
    this.#actor.start();
    this.#lifecycle.addEventListener('visibilitychange', this.#wake);
    this.#lifecycle.addEventListener('online', this.#wake);
    this.#lifecycle.addEventListener('offline', this.#offline);
  }

  getState = (): WebSessionState => this.#view;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  /** Load the snapshot, then subscribe to the stream; settles once the first subscription does. */
  start(): Promise<void> {
    if (this.#started === null) {
      this.#started = new Promise(resolve => {
        this.#startupSettled = resolve;
      });
      this.#send({type: 'start'});
    }
    return this.#started;
  }

  close(): Promise<void> {
    if (this.#closed === null) this.#send({type: 'close'});
    return this.#closed ?? Promise.resolve();
  }

  /** Reload the run and restart the stream from a fresh bootstrap (the Reattach button). */
  reattach(): void {
    this.#send({type: 'reattach', reach: this.#reach(), runEnded: this.#runEnded()});
  }

  /** Redial the control channel now (the Reconnect button); the event stream is untouched. */
  reconnectControls(): void {
    this.#send({type: 'reconnect-controls'});
  }

  /** Send to the machine while it runs; a closed session drops every later report. */
  #send(event: SessionEvent): void {
    if (this.#actor.getSnapshot().status === 'active') this.#actor.send(event);
  }

  #reach(): Reachability {
    return {visible: this.#lifecycle.visibilityState !== 'hidden', online: this.#lifecycle.online};
  }

  #runEnded(): boolean {
    return hasRunEnded(this.store.getState());
  }

  #wake = (): void => this.#send({type: 'wake', reach: this.#reach()});

  #offline = (): void => this.#send({type: 'offline', runEnded: this.#runEnded()});

  /** The machine with its operations and actions performed through the run connection. */
  #provided() {
    return sessionMachine.provide({
      actors: {
        loadSnapshot: fromPromise<SnapshotResult, void>(() => this.#loadSnapshot()),
        subscribe: fromPromise<Error | null, void>(() => this.#subscribe()),
        refresh: fromPromise<RefreshOutcome, void>(() => this.#refresh()),
      },
      actions: {
        applySnapshot: (_, {snapshot}) => {
          if (snapshot !== null) this.store.dispatch({type: 'snapshot', snapshot});
        },
        recoverStream: (_, {mode}) => {
          try {
            if (mode === 'rebootstrap') this.#connection.rebootstrap();
            else this.#connection.retry();
          } catch (error) {
            // Sent to the machine's own mailbox: it is handled after the current step.
            this.#send({type: 'fault', error: toError(error)});
          }
        },
        redialControls: () => this.#connection.redialControls(),
        closeShell: () => {
          this.#lifecycle.removeEventListener('visibilitychange', this.#wake);
          this.#lifecycle.removeEventListener('online', this.#wake);
          this.#lifecycle.removeEventListener('offline', this.#offline);
          this.#closed = this.#connection.close();
        },
      },
    });
  }

  /** Subscribe to the run's event stream; resolves to why it failed, or null. */
  async #subscribe(): Promise<Error | null> {
    try {
      await this.#connection.subscribe({
        cursor: () => this.store.getState().sequence,
        shouldReconnect: () => !this.#runEnded(),
        onBatch: (message, reconciliation) =>
          this.store.dispatch({type: 'batch', message, reconciliation}),
        onFault: error => this.#send({type: 'fault', error}),
        onConnectionState: state => this.#send({type: 'stream', state}),
      });
      return null;
    } catch (error) {
      return toError(error);
    }
  }

  /**
   * A browser wake is stronger evidence than either reconnect timer: a background tab may have had
   * both coalesced indefinitely. The redial is inert on a healthy or dialing channel, and otherwise
   * dials before the snapshot request is queued.
   */
  async #refresh(): Promise<RefreshOutcome> {
    try {
      this.#connection.redialControls();
    } catch (error) {
      return {kind: 'redial-failed', error: toError(error)};
    }
    return {kind: 'snapshot', result: await this.#loadSnapshot()};
  }

  /** Load the snapshot; the machine decides whether the run fold applies it. */
  async #loadSnapshot(): Promise<SnapshotResult> {
    try {
      return {ok: true, snapshot: await this.#connection.loadSnapshot()};
    } catch (error) {
      return {ok: false, error: toError(error)};
    }
  }
}
