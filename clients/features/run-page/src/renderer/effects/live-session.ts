/**
 * The attached run's session, as the Run screen's store sees it: opened when the window has
 * attached the live run (`link` is `attached`), closed when the link goes, and reporting what it
 * says and folds as actions. The session itself (bootstrap, stream, recovery) is
 * `@vibesys/run-session`'s machine; this keeps exactly one open per attached server.
 */
import type {CoreState} from '@vibesys/run-state';
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
  | {readonly kind: 'resume'; readonly when: ResumeWhen};

/** One open session: a command it delivers, and its close. */
export interface LiveSession {
  /** Send `command`; resolves null once acknowledged, or why the run refused it. */
  command(command: SessionCommand): Promise<string | null>;
  close(): void;
}

/** Opens one session to the attached run, reporting to `observer`. */
export type OpenSession = (observer: SessionObserver) => LiveSession;

/** Keeps the session in step with the store's link. */
export class SessionHolder {
  #open: {readonly instance: string; readonly session: LiveSession} | null = null;

  constructor(
    private readonly openSession: OpenSession,
    private readonly dispatch: (action: RunPageAction) => void,
    /** This window's clock (epoch ms), which stamps when the run's data arrived. */
    private readonly now: () => number,
  ) {}

  reconcile(state: RunPageState): void {
    const wanted = state.link.kind === 'attached' ? state.link.instance : null;
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
