/**
 * The bundled app's one window: the welcome view (trusted, the welcome preload) and, once a run is
 * attached, the run page (the web UI, the run preload) under the welcome view's title strip.
 *
 * The window lays the views out, switches between them, and carries messages to them; it decides
 * nothing about runs or hosts. The run page names no host, path, or command: each `connect()` of its
 * preload hands this process a MessagePort, which the window passes on only when it comes from the
 * current run page.
 */

import type {RelayPort} from '@vibesys/feature-run-control/main';
import {
  type ChromeState,
  CONNECT_CHANNEL,
  LOGS_CHANNEL,
  type RunLogsReply,
  type RunLogsRequest,
  WAKE_CHANNEL,
  type WelcomeEvent,
} from '@vibesys/ipc';
import {type RunRef, sameRun} from '@vibesys/runs';
import {BaseWindow, dialog, ipcMain, type MessagePortMain, type WebContentsView} from 'electron';
import {APP_ENTRY_URL, isSameAppPage, SHELL_URL} from './app-assets.js';
import {sandboxedView} from './app-security.js';
import {type PendingRunPort, RunIpcRegistry} from './run-ipc.js';
import {observeRunPage, RunPageLifecycle} from './run-page-lifecycle.js';
import type {WelcomeDocument} from './welcome-ipc.js';
import {
  observeWelcomePage,
  WelcomePageLifecycle,
  type WelcomePageLoss,
} from './welcome-page-lifecycle.js';
import {TITLEBAR_HEIGHT, windowChrome} from './window-chrome.js';

/**
 * The one process-wide run-connect registry every window competes through.
 *
 * Lazily built from whichever window constructs first: every window in this process uses the same
 * `log`, so which one happens to create the registry is not observable.
 */
let sharedRunIpc: RunIpcRegistry | null = null;
function runIpcRegistry(log: (message: string) => void): RunIpcRegistry {
  sharedRunIpc ??= new RunIpcRegistry(
    {
      onConnect: listener => {
        ipcMain.on(CONNECT_CHANNEL, event => {
          const port = event.ports[0];
          listener({
            port: port === undefined ? null : pendingRunPort(port),
            senderId: event.sender.id,
            senderUrl: event.senderFrame?.url ?? null,
          });
        });
      },
      onLogs: listener => {
        ipcMain.handle(LOGS_CHANNEL, (event, request: unknown) =>
          listener({
            request,
            senderId: event.sender.id,
            senderUrl: event.senderFrame?.url ?? null,
          }),
        );
      },
    },
    [APP_ENTRY_URL, SHELL_URL],
    log,
  );
  return sharedRunIpc;
}

export interface AppWindowDeps {
  readonly log: (message: string) => void;
  /** The page the trusted surface loads: the welcome view or the app shell (`surfaceUrl`). */
  readonly surfaceUrl: string;
  /** The run page opened one connection, for the run bound to it (`openRunPage`'s `run`). */
  readonly connect: (port: RelayPort, run: RunRef) => void;
  /** The run page asked about its run's logs (`openRunPage`'s `run`). */
  readonly logs: (run: RunRef, request: RunLogsRequest) => Promise<RunLogsReply<RunLogsRequest>>;
  /** The native window or its current run page disappeared: detach the run bound to it. */
  readonly closed: (run: RunRef) => void;
  /** The welcome surface exhausted its bounded recovery; enter fatal app shutdown. */
  readonly fatal: (error: Error) => void;
  /** The native window's title before any run page names one; `VibeSys` by default. */
  readonly title?: string;
  /** The native window closed. */
  readonly windowClosed?: () => void;
}

function pendingRunPort(port: MessagePortMain): PendingRunPort {
  return {
    start: () => port.start(),
    post: message => port.postMessage(message),
    onMessage: listener => port.on('message', event => listener(event.data)),
    onClose: listener => port.on('close', listener),
    close: () => port.close(),
  };
}

export class AppWindow {
  readonly #deps: AppWindowDeps;
  readonly #window: BaseWindow;
  readonly #runPages: RunPageLifecycle<WebContentsView>;
  readonly #welcomePages: WelcomePageLifecycle<WebContentsView>;
  readonly #welcomeNewDocuments = new Set<(generation: string | null) => void>();
  readonly #welcomeDestroyed = new Set<() => void>();
  /** Stable welcome-document authority across the one allowed WebContentsView recreation. */
  readonly welcome: WelcomeDocument;
  #mode: ChromeState['mode'] = 'welcome';
  /**
   * Whether the trusted surface shows runs itself (the app shell, `SHELL_URL`): then a bound run
   * gets no run page; its connections come from the surface's own document.
   */
  readonly #runsInSurface: boolean;
  /** The run bound to the current run page, if any; null before one ever opened. */
  #currentRun: RunRef | null = null;
  /** Withdraws this window's admission claim from the shared run-connect registry. */
  readonly #unregisterRunIpc: () => void;

  constructor(deps: AppWindowDeps) {
    this.#deps = deps;
    // The app shell's page, with or without a run window's query, shows runs in its own document.
    this.#runsInSurface = isSameAppPage(deps.surfaceUrl, SHELL_URL);
    this.#window = new BaseWindow({
      width: 1440,
      height: 900,
      minWidth: 960,
      minHeight: 600,
      title: deps.title ?? 'VibeSys',
      ...windowChrome(process.platform),
    });
    this.#runPages = new RunPageLifecycle({
      retire: view => this.#retireRunPage(view),
      showWelcome: () => this.#showWelcome(),
      currentPageLost: () => this.#detachCurrentRun(),
    });
    this.#welcomePages = new WelcomePageLifecycle({
      recreate: () => sandboxedView('welcome.cjs'),
      activate: (view, lost) => this.#activateWelcomePage(view, lost),
      retire: view => this.#retireWelcomePage(view),
      documentRetired: () => {
        for (const listener of [...this.#welcomeNewDocuments]) listener(null);
      },
      recovering: reason => deps.log(`recovering the welcome view after ${reason}`),
      fatal: reason => deps.fatal(new Error(`could not recover the welcome view after ${reason}`)),
    });
    this.welcome = {
      currentId: () => this.#welcomePages.current?.webContents.id ?? null,
      onNewDocument: listener => this.#welcomeNewDocuments.add(listener),
      onDestroyed: listener => this.#welcomeDestroyed.add(listener),
    };
    this.#window.on('resize', () => this.#layout());
    this.#window.on('closed', () => {
      this.#runPages.abandon();
      this.#welcomePages.abandon();
      for (const listener of [...this.#welcomeDestroyed]) listener();
      this.#detachCurrentRun();
      this.#unregisterRunIpc();
      deps.windowClosed?.();
    });
    // A new surface document has not attached the run the old one showed: let that run go.
    if (this.#runsInSurface) this.#welcomeNewDocuments.add(() => this.#detachCurrentRun());
    this.#welcomePages.start(sandboxedView('welcome.cjs'));
    this.#unregisterRunIpc = runIpcRegistry(deps.log).register({
      currentSenderId: () => this.#runSenderId(),
      // The registry only admits a port whose sender is this window's current run page, so
      // `#currentRun` names the same run this port's connection is for.
      connect: port => {
        const run = this.#currentRun;
        if (run === null) {
          port.close();
          return;
        }
        this.#deps.connect(port, run);
      },
      logs: request => {
        const run = this.#currentRun;
        if (run === null) return Promise.reject(new Error('this window shows no run'));
        return this.#deps.logs(run, request);
      },
    });
  }

  get mode(): ChromeState['mode'] {
    return this.#mode;
  }

  /** Show `mode`'s view; without a run page, always the welcome view. */
  setMode(mode: ChromeState['mode']): void {
    this.#mode = this.#runPages.current === null ? 'welcome' : mode;
    this.#layout();
  }

  /**
   * Replace the run page with a new one for `run`, and show it.
   *
   * This app shows one run page at a time: opening a second run's page here always replaces the
   * first, the same way it always has. `run` lets this window tell IPC admission and the
   * `closed`/`connect` callbacks which run its one page is for, so run-control's binding for a run
   * that is not the one currently shown is never reached by this window's events.
   */
  openRunPage(
    run: RunRef,
    title: {readonly hostLabel: string; readonly task: string | null},
  ): {
    wake(): void;
    close(): void;
  } {
    if (this.#runsInSurface) return this.#bindSurfaceRun(run);
    const view = sandboxedView('run.cjs');
    this.#runPages.replace(view);
    this.#currentRun = run;
    this.#window.contentView.addChildView(view);
    this.#window.setTitle(`VibeSys · ${title.hostLabel}`);
    void observeRunPage(
      view.webContents,
      APP_ENTRY_URL,
      () => this.#runPages.lost(view),
      () => this.#deps.log('could not load the run view'),
    );
    this.setMode('run');
    return {
      wake: () => {
        if (!view.webContents.isDestroyed()) view.webContents.send(WAKE_CHANNEL);
      },
      close: () => {
        if (this.#currentRun !== null && sameRun(this.#currentRun, run)) this.#currentRun = null;
        this.#runPages.close(view);
      },
    };
  }

  /**
   * The app shell asked to let its run go (it shows another screen): detach the run bound to the
   * surface. Does nothing when the surface binds no run, or when runs have their own page.
   */
  releaseSurfaceRun(): void {
    if (this.#runsInSurface) this.#detachCurrentRun();
  }

  /**
   * Bind `run` to the surface itself: the app shell shows the run in its own document, so no run
   * page opens and the layout stays. A different run bound before is detached first, as replacing a
   * run page detaches its run.
   */
  #bindSurfaceRun(run: RunRef): {wake(): void; close(): void} {
    if (this.#currentRun === null || !sameRun(this.#currentRun, run)) this.#detachCurrentRun();
    this.#currentRun = run;
    return {
      wake: () => {
        const surface = this.#welcomePages.current;
        if (surface !== null && !surface.webContents.isDestroyed()) {
          surface.webContents.send(WAKE_CHANNEL);
        }
      },
      close: () => {
        if (this.#currentRun !== null && sameRun(this.#currentRun, run)) this.#currentRun = null;
      },
    };
  }

  /** The web contents whose run connections this window admits: its run page, or the surface. */
  #runSenderId(): number | null {
    if (!this.#runsInSurface) return this.#runPages.current?.webContents.id ?? null;
    return this.#currentRun === null ? null : (this.#welcomePages.current?.webContents.id ?? null);
  }

  /** Send one ordered `event` to the welcome view on `channel`. */
  sendWelcome<T>(channel: string, event: WelcomeEvent<T>): void {
    const welcome = this.#welcomePages.current;
    if (welcome !== null && !welcome.webContents.isDestroyed()) {
      welcome.webContents.send(channel, event);
    }
  }

  /** Ask the user a stop question; resolves true on "Stop". */
  async confirmStop(text: string): Promise<boolean> {
    const {response} = await dialog.showMessageBox(this.#window, {
      type: 'warning',
      message: text,
      buttons: ['Cancel', 'Stop'],
      defaultId: 0,
      cancelId: 0,
    });
    return response === 1;
  }

  /** Bring the window to the front (a second launch of the app asked for it). */
  focus(): void {
    if (this.#window.isDestroyed()) return;
    if (this.#window.isMinimized()) this.#window.restore();
    this.#window.show();
    this.#window.focus();
  }

  /** Close this window, as the user closing it would: `'closed'` detaches its current run. */
  close(): void {
    if (!this.#window.isDestroyed()) this.#window.close();
  }

  #retireRunPage(view: WebContentsView): void {
    if (!this.#window.isDestroyed()) this.#window.contentView.removeChildView(view);
    if (!view.webContents.isDestroyed()) view.webContents.close();
  }

  #activateWelcomePage(view: WebContentsView, lost: (reason: WelcomePageLoss) => void): void {
    this.#window.contentView.addChildView(view);
    view.webContents.on('did-start-navigation', event => {
      if (this.#welcomePages.current === view && event.isMainFrame && !event.isSameDocument) {
        // Electron reports the frame that is beginning the new document here. Its routing
        // identity may already be the successor's, so it must not be named as the generation
        // being retired. The document owner already tracks every active predecessor generation.
        for (const listener of [...this.#welcomeNewDocuments]) listener(null);
      }
    });
    this.#layout();
    observeWelcomePage(view.webContents, this.#deps.surfaceUrl, lost);
  }

  #retireWelcomePage(view: WebContentsView): void {
    if (!this.#window.isDestroyed()) this.#window.contentView.removeChildView(view);
    if (!view.webContents.isDestroyed()) view.webContents.close();
  }

  /** Report the current run page's loss, if it has one; safe before any run page ever opened. */
  #detachCurrentRun(): void {
    const run = this.#currentRun;
    if (run === null) return;
    this.#currentRun = null;
    this.#deps.closed(run);
  }

  #showWelcome(): void {
    if (!this.#window.isDestroyed()) this.#window.setTitle('VibeSys');
    this.setMode('welcome');
  }

  /** Lay the views out: the filling view, and the welcome view's strip over a run. */
  #layout(): void {
    if (this.#window.isDestroyed()) return;
    const {width, height} = this.#window.getContentBounds();
    const full = {x: 0, y: 0, width, height};
    const run = this.#runPages.current;
    if (run !== null) {
      run.setBounds(full);
      run.setVisible(this.#mode === 'run');
    }
    const welcome = this.#welcomePages.current;
    if (welcome !== null) {
      welcome.setBounds(this.#mode === 'run' ? {...full, height: TITLEBAR_HEIGHT} : full);
      // Re-adding moves the welcome view (the title strip) above the run view.
      this.#window.contentView.addChildView(welcome);
    }
  }
}
