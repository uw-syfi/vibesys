/**
 * One native window: its trusted surface, the app shell (`shell.html`, under the welcome preload),
 * filling it. A run is shown by the shell's own document, so a window binds at most one run to its
 * surface at a time; the app has a main window and one more per run opened in a window of its own.
 *
 * The window lays the surface out and carries messages to it; it decides nothing about runs or
 * hosts. The surface names no host, path, or command: each `connectRun` of its preload hands this
 * process a MessagePort, which the registry (`run-ipc.ts`) admits only when it comes from the
 * document of a window that has a run bound.
 */

import type {RelayPort} from '@vibesys/feature-run-control/main';
import {
  CHROME_INSET_CHANNEL,
  CONNECT_CHANNEL,
  OPEN_SETTINGS_CHANNEL,
  WAKE_CHANNEL,
  type WelcomeEvent,
} from '@vibesys/ipc';
import {type RunRef, sameRun} from '@vibesys/runs';
import {BaseWindow, ipcMain, type MessagePortMain, type WebContentsView} from 'electron';
import {SHELL_URL} from './app-assets.js';
import {sandboxedView} from './app-security.js';
import {type PendingRunPort, RunIpcRegistry} from './run-ipc.js';
import type {WelcomeDocument} from './welcome-ipc.js';
import {
  observeWelcomePage,
  WelcomePageLifecycle,
  type WelcomePageLoss,
} from './welcome-page-lifecycle.js';
import {chromeInset, windowChrome} from './window-chrome.js';

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
    },
    SHELL_URL,
    log,
  );
  return sharedRunIpc;
}

export interface AppWindowDeps {
  readonly log: (message: string) => void;
  /** The page the surface loads: the app shell, with a run's query in a run window. */
  readonly surfaceUrl: string;
  /** The surface opened one connection, for the run bound to the window (`bindRun`'s `run`). */
  readonly connect: (port: RelayPort, run: RunRef) => void;
  /** The native window or its surface document disappeared: detach the run bound to it. */
  readonly closed: (run: RunRef) => void;
  /** The surface exhausted its bounded recovery; enter fatal app shutdown. */
  readonly fatal: (error: Error) => void;
  /** The native window's title; `VibeSys` by default. */
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
  readonly #pages: WelcomePageLifecycle<WebContentsView>;
  readonly #newDocuments = new Set<(generation: string | null) => void>();
  readonly #destroyed = new Set<() => void>();
  /** Stable surface-document authority across the one allowed WebContentsView recreation. */
  readonly welcome: WelcomeDocument;
  /** The run bound to the surface, if any; null before one ever bound. */
  #currentRun: RunRef | null = null;
  /** Withdraws this window's admission claim from the shared run-connect registry. */
  readonly #unregisterRunIpc: () => void;

  constructor(deps: AppWindowDeps) {
    this.#deps = deps;
    this.#window = new BaseWindow({
      width: 1440,
      height: 900,
      minWidth: 960,
      minHeight: 600,
      title: deps.title ?? 'VibeSys',
      ...windowChrome(process.platform),
    });
    this.#pages = new WelcomePageLifecycle({
      recreate: () => sandboxedView('welcome.cjs'),
      activate: (view, lost) => this.#activate(view, lost),
      retire: view => this.#retire(view),
      documentRetired: () => {
        for (const listener of [...this.#newDocuments]) listener(null);
      },
      recovering: reason => deps.log(`recovering the shell after ${reason}`),
      fatal: reason => deps.fatal(new Error(`could not recover the shell after ${reason}`)),
    });
    this.welcome = {
      currentId: () => this.#pages.current?.webContents.id ?? null,
      onNewDocument: listener => this.#newDocuments.add(listener),
      onDestroyed: listener => this.#destroyed.add(listener),
    };
    this.#window.on('resize', () => this.#layout());
    // Full screen hides the window controls, so the page reclaims the room they take.
    this.#window.on('enter-full-screen', () => this.#sendChromeInset());
    this.#window.on('leave-full-screen', () => this.#sendChromeInset());
    this.#window.on('closed', () => {
      this.#pages.abandon();
      for (const listener of [...this.#destroyed]) listener();
      this.#detachCurrentRun();
      this.#unregisterRunIpc();
      deps.windowClosed?.();
    });
    // A new surface document has not attached the run the old one showed: let that run go.
    this.#newDocuments.add(() => this.#detachCurrentRun());
    this.#pages.start(sandboxedView('welcome.cjs'));
    this.#unregisterRunIpc = runIpcRegistry(deps.log).register({
      currentSenderId: () =>
        this.#currentRun === null ? null : (this.#pages.current?.webContents.id ?? null),
      // The registry only admits a port whose sender is this window's surface while a run is
      // bound, so `#currentRun` names the same run this port's connection is for.
      connect: port => {
        const run = this.#currentRun;
        if (run === null) {
          port.close();
          return;
        }
        this.#deps.connect(port, run);
      },
    });
  }

  /**
   * Bind `run` to the surface: the shell shows the run in its own document, so the layout stays.
   * A different run bound before is detached first. The returned handle wakes the surface's session
   * and unbinds the run.
   */
  bindRun(run: RunRef): {wake(): void; close(): void} {
    if (this.#currentRun === null || !sameRun(this.#currentRun, run)) this.#detachCurrentRun();
    this.#currentRun = run;
    return {
      wake: () => {
        const surface = this.#pages.current;
        if (surface !== null && !surface.webContents.isDestroyed()) {
          surface.webContents.send(WAKE_CHANNEL);
        }
      },
      close: () => {
        if (this.#currentRun !== null && sameRun(this.#currentRun, run)) this.#currentRun = null;
      },
    };
  }

  /** The shell asked to let its run go (it shows another screen): detach the bound run, if any. */
  releaseSurfaceRun(): void {
    this.#detachCurrentRun();
  }

  /** Send one ordered `event` to the surface on `channel`. */
  sendWelcome<T>(channel: string, event: WelcomeEvent<T>): void {
    const surface = this.#pages.current;
    if (surface !== null && !surface.webContents.isDestroyed()) {
      surface.webContents.send(channel, event);
    }
  }

  /** Tell the main shell to show Settings, including requests from a run window. */
  openSettings(): void {
    const surface = this.#pages.current;
    if (surface !== null && !surface.webContents.isDestroyed()) {
      surface.webContents.send(OPEN_SETTINGS_CHANNEL);
    }
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

  #activate(view: WebContentsView, lost: (reason: WelcomePageLoss) => void): void {
    this.#window.contentView.addChildView(view);
    view.webContents.on('did-start-navigation', event => {
      if (this.#pages.current === view && event.isMainFrame && !event.isSameDocument) {
        // Electron reports the frame that is beginning the new document here. Its routing
        // identity may already be the successor's, so it must not be named as the generation
        // being retired. The document owner already tracks every active predecessor generation.
        for (const listener of [...this.#newDocuments]) listener(null);
      }
    });
    // The first paint assumes a window that is not full screen; a document loaded in one corrects it.
    view.webContents.on('did-finish-load', () => this.#sendChromeInset());
    this.#layout();
    observeWelcomePage(view.webContents, this.#deps.surfaceUrl, lost);
  }

  #retire(view: WebContentsView): void {
    if (!this.#window.isDestroyed()) this.#window.contentView.removeChildView(view);
    if (!view.webContents.isDestroyed()) view.webContents.close();
  }

  /** Report the bound run's loss, if it has one; safe before any run ever bound. */
  #detachCurrentRun(): void {
    const run = this.#currentRun;
    if (run === null) return;
    this.#currentRun = null;
    this.#deps.closed(run);
  }

  /** Tell the surface what the window's controls cover now. */
  #sendChromeInset(): void {
    const surface = this.#pages.current;
    if (this.#window.isDestroyed() || surface === null || surface.webContents.isDestroyed()) return;
    surface.webContents.send(
      CHROME_INSET_CHANNEL,
      chromeInset(process.platform, this.#window.isFullScreen()),
    );
  }

  /** The surface fills the window. */
  #layout(): void {
    if (this.#window.isDestroyed()) return;
    const surface = this.#pages.current;
    if (surface === null) return;
    const {width, height} = this.#window.getContentBounds();
    surface.setBounds({x: 0, y: 0, width, height});
  }
}
