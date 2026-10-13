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
  type RunChannelRole,
  WAKE_CHANNEL,
  type WelcomeEvent,
} from '@vibesys/ipc';
import {
  BaseWindow,
  dialog,
  ipcMain,
  type MessagePortMain,
  nativeTheme,
  type WebContentsView,
} from 'electron';
import {APP_ENTRY_URL} from './app-assets.js';
import {sandboxedView} from './app-security.js';
import {bindRunIpc, type PendingRunPort} from './run-ipc.js';
import {observeRunPage, RunPageLifecycle} from './run-page-lifecycle.js';
import {TITLEBAR_HEIGHT, windowChrome} from './window-chrome.js';

export const WELCOME_URL = 'app://vibesys/welcome.html';

export interface AppWindowDeps {
  readonly log: (message: string) => void;
  /** The run page opened one connection. */
  readonly connect: (port: RelayPort, role: RunChannelRole) => void;
  /** The native window or its current run page disappeared: detach the attached run. */
  readonly closed: () => void;
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
  readonly welcome: WebContentsView;
  #mode: ChromeState['mode'] = 'welcome';

  constructor(deps: AppWindowDeps) {
    this.#deps = deps;
    this.#window = new BaseWindow({
      width: 1440,
      height: 900,
      minWidth: 960,
      minHeight: 600,
      title: 'VibeSys',
      ...windowChrome(process.platform),
    });
    this.#runPages = new RunPageLifecycle({
      retire: view => this.#retireRunPage(view),
      showWelcome: () => this.#showWelcome(),
      currentPageLost: () => this.#deps.closed(),
    });
    this.welcome = sandboxedView('welcome.cjs');
    this.#window.contentView.addChildView(this.welcome);
    this.#window.on('resize', () => this.#layout());
    this.#window.on('closed', () => {
      this.#runPages.abandon();
      if (!this.welcome.webContents.isDestroyed()) this.welcome.webContents.close();
      this.#deps.closed();
    });
    this.#layout();
    this.welcome.webContents
      .loadURL(WELCOME_URL)
      .catch(() => deps.log('could not load the welcome view'));
    bindRunIpc(
      {
        onConnect: listener => {
          ipcMain.on(CONNECT_CHANNEL, (event, value: unknown) => {
            const port = event.ports[0];
            listener({
              port: port === undefined ? null : pendingRunPort(port),
              senderId: event.sender.id,
              senderUrl: event.senderFrame?.url ?? null,
              value,
            });
          });
        },
      },
      APP_ENTRY_URL,
      () => this.#runPages.current?.webContents.id ?? null,
      deps.connect,
      deps.log,
    );
  }

  get mode(): ChromeState['mode'] {
    return this.#mode;
  }

  /** Show `mode`'s view; without a run page, always the welcome view. */
  setMode(mode: ChromeState['mode']): void {
    this.#mode = this.#runPages.current === null ? 'welcome' : mode;
    this.#layout();
  }

  /** Replace the run page with a new one for a run on `hostLabel`, and show it. */
  openRunPage(hostLabel: string): {wake(): void; close(): void} {
    const view = sandboxedView('run.cjs');
    this.#runPages.replace(view);
    this.#window.contentView.addChildView(view);
    this.#window.setTitle(`VibeSys · ${hostLabel}`);
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
        this.#runPages.close(view);
      },
    };
  }

  /** Send one ordered `event` to the welcome view on `channel`. */
  sendWelcome<T>(channel: string, event: WelcomeEvent<T>): void {
    if (!this.welcome.webContents.isDestroyed()) this.welcome.webContents.send(channel, event);
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

  #retireRunPage(view: WebContentsView): void {
    if (!this.#window.isDestroyed()) this.#window.contentView.removeChildView(view);
    if (!view.webContents.isDestroyed()) view.webContents.close();
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
    // The run page follows one dark theme; the welcome view follows the system's.
    nativeTheme.themeSource = this.#mode === 'run' ? 'dark' : 'system';
    const run = this.#runPages.current;
    if (run !== null) {
      run.setBounds(full);
      run.setVisible(this.#mode === 'run');
    }
    this.welcome.setBounds(this.#mode === 'run' ? {...full, height: TITLEBAR_HEIGHT} : full);
    // Re-adding moves the welcome view (the title strip) above the run view.
    this.#window.contentView.addChildView(this.welcome);
  }
}
