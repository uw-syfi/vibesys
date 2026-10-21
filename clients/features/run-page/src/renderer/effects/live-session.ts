/**
 * The attached run's session, as the Run screen's store sees it: opened when the window has
 * attached the live run (`link` is `attached`), closed when the link goes, and reporting what it
 * says and folds as actions. The session itself (bootstrap, stream, recovery) is
 * `@vibesys/run-session`'s machine; this keeps exactly one open per attached server.
 */
import type {RunCommand} from '@vibesys/run-session';
import type {CoreState} from '@vibesys/run-state';
import type {AbortSignalLike} from '@vibesys/transport';
import type {
  ResumeWhen,
  RunData,
  RunPageAction,
  RunPageState,
  SessionView,
} from '../../state/index.js';

/** What a live session reports to the store. */
export interface SessionObserver {
  session(view: SessionView): void;
  core(core: CoreState): void;
  data(data: RunData): void;
}

/** A command the Run screen sends over the session: Pause, or Resume now or when capacity returns. */
export type SessionCommand =
  | {readonly kind: 'pause'}
  | {readonly kind: 'pause-now'}
  | {readonly kind: 'resume'; readonly when: ResumeWhen};

/** What a chat request came to: it went through, or why it did not. */
export type ChatReply<T> =
  | ({readonly ok: true} & T)
  | {readonly ok: false; readonly message: string};

/** A `command.control` request, as the run protocol takes it. */
export type ControlInput = Extract<RunCommand, {readonly type: 'command.control'}>['control'];

/** What the run did with a control: took it, or refused it. */
export type ControlReply =
  | {readonly kind: 'acknowledged'}
  | {readonly kind: 'rejected'; readonly message: string | null; readonly code?: string | null};

/** The run's chat over its session: the panel's only way to the run. */
export interface LiveChat {
  /** Put `text` to `threadId`; the answer streams into the run fold as events. */
  ask(
    input: {readonly threadId: string; readonly text: string},
    signal: AbortSignalLike,
  ): Promise<ChatReply<unknown>>;
  /** Create a thread on `harness` (null: the run's own). */
  createThread(
    harness: {readonly provider: string; readonly model: string} | null,
    signal: AbortSignalLike,
  ): Promise<ChatReply<{readonly threadId: string}>>;
  /** Send one control (a steer or its withdrawal). */
  control(request: ControlInput): Promise<ControlReply>;
}

/** One open session: a command it delivers, its chat, and its close. */
export interface LiveSession {
  readonly chat: LiveChat;
  /** Send `command`; resolves null once acknowledged, or why the run refused it. */
  command(command: SessionCommand): Promise<string | null>;
  close(): void;
}

/** Opens one session to the attached run, reporting to `observer`. */
export type OpenSession = (observer: SessionObserver) => LiveSession;

const CONNECTING = {ok: false, message: 'the run is still connecting'} as const;
const NOT_CONNECTED = {ok: false, message: 'the run is not connected'} as const;

/** Keeps the session in step with the store's link. */
export class SessionHolder {
  #open: {readonly instance: string; readonly session: LiveSession} | null = null;

  constructor(
    private readonly openSession: OpenSession,
    private readonly dispatch: (action: RunPageAction) => void,
    /** This window's clock (epoch ms), which stamps when the run's data arrived. */
    private readonly now: () => number,
    /** The server instance the state wants a session to; null: none. */
    private readonly wanted: (state: RunPageState) => string | null,
  ) {}

  reconcile(state: RunPageState): void {
    const wanted = this.wanted(state);
    if (this.#open?.instance === wanted) return;
    this.close();
    if (wanted === null) return;
    // A closed session's late reports are dropped here, before they reach the store.
    let current = true;
    let session: LiveSession | null = null;
    // Held before the session opens: opening reports at once, and each report reconciles again.
    this.#open = {
      instance: wanted,
      session: {
        chat: {
          ask: (input, signal) =>
            session === null ? Promise.resolve(CONNECTING) : session.chat.ask(input, signal),
          createThread: (harness, signal) =>
            session === null
              ? Promise.resolve(CONNECTING)
              : session.chat.createThread(harness, signal),
          control: request =>
            session === null
              ? Promise.resolve({kind: 'rejected', message: CONNECTING.message})
              : session.chat.control(request),
        },
        command: command =>
          session === null
            ? Promise.resolve('the run is still connecting')
            : session.command(command),
        close: () => {
          current = false;
          session?.close();
        },
      },
    };
    const report = (action: RunPageAction): void => {
      if (current) this.dispatch(action);
    };
    const opened = this.openSession({
      session: view => report({type: 'session', session: view}),
      core: core => report({type: 'run-core', core}),
      data: data => report({type: 'run-data', data, wallMs: this.now()}),
    });
    session = opened;
    // Closed while it was opening (a report moved the link on): close what just opened.
    if (!current) opened.close();
  }

  /** Whether a session is open. */
  isOpen(): boolean {
    return this.#open !== null;
  }

  /** The attached session's chat; before one is attached every request says so. */
  readonly chat: LiveChat = {
    ask: (input, signal) =>
      this.#open?.session.chat.ask(input, signal) ?? Promise.resolve(NOT_CONNECTED),
    createThread: (harness, signal) =>
      this.#open?.session.chat.createThread(harness, signal) ?? Promise.resolve(NOT_CONNECTED),
    control: request =>
      this.#open?.session.chat.control(request) ??
      Promise.resolve({kind: 'rejected', message: 'the run is not connected'}),
  };

  command(command: SessionCommand): Promise<string | null> {
    const open = this.#open;
    if (open === null) return Promise.resolve('the run is not connected');
    return open.session.command(command);
  }

  close(): void {
    const open = this.#open;
    this.#open = null;
    open?.session.close();
  }
}
