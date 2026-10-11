/**
 * The bundled app's one window: the welcome view (trusted, the welcome preload) and, once a run is
 * attached, the run page (the web UI, the run preload) under the welcome view's title strip.
 *
 * The window lays the views out, switches between them, and carries messages to them; it decides
 * nothing about runs or hosts. The run page names no host, path, or command: each `connect()` of its
 * preload hands this process a MessagePort, which the window passes on only when it comes from the
 * current run page.
 */

import {type ChromeState, CONNECT_CHANNEL, WAKE_CHANNEL, type WelcomeEvent} from '@vibesys/ipc';
import {
  BaseWindow,
  dialog,
  ipcMain,
  type MessagePortMain,
  nativeTheme,
  type WebContentsView,
} from 'electron';
import type {RelayPort} from '../features/run-control/index.js';
import {APP_ENTRY_URL, isAppPage} from './app-assets.js';
import {sandboxedView} from './app-security.js';
import {TITLEBAR_HEIGHT, windowChrome} from './window-chrome.js';

export const WELCOME_URL = 'app://vibesys/welcome.html';

export interface AppWindowDeps {
  readonly log: (message: string) => void;
  /** The run page opened one connection. */
  readonly connect: (port: RelayPort) => void;
  /** The window closed: the attached run's page is gone. */
  readonly closed: () => void;
}

function relayPort(port: MessagePortMain): RelayPort {
  return {
    post: message => port.postMessage(message),
    onMessage: listener => port.on('message', event => listener(event.data)),
    onClose: listener => port.on('close', listener),
    close: () => port.close(),
  };
}

export class AppWindow {
  readonly #deps: AppWindowDeps;
  readonly #window: BaseWindow;
  readonly welcome: WebContentsView;
  #run: WebContentsView | null = null;
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
    this.welcome = sandboxedView('welcome.cjs');
    this.#window.contentView.addChildView(this.welcome);
    this.#window.on('resize', () => this.#layout());
    this.#window.on('closed', () => deps.closed());
    this.#layout();
    this.welcome.webContents
      .loadURL(WELCOME_URL)
      .catch(() => deps.log('could not load the welcome view'));
    ipcMain.on(CONNECT_CHANNEL, event => {
      const [port] = event.ports;
      if (port === undefined) return;
      const run = this.#run;
      const url = event.senderFrame?.url ?? '';
      if (run === null || event.sender.id !== run.webContents.id || !isAppPage(url)) {
        deps.log('refused a connection from a view that is not bound to a run');
        port.close();
        return;
      }
      port.start();
      deps.connect(relayPort(port));
    });
  }

  get mode(): ChromeState['mode'] {
    return this.#mode;
  }

  /** Show `mode`'s view; without a run page, always the welcome view. */
  setMode(mode: ChromeState['mode']): void {
    this.#mode = this.#run === null ? 'welcome' : mode;
    this.#layout();
  }

  /** Replace the run page with a new one for a run on `hostLabel`, and show it. */
  openRunPage(hostLabel: string): {wake(): void; close(): void} {
    this.#closeRunPage();
    const view = sandboxedView('run.cjs');
    this.#run = view;
    this.#window.contentView.addChildView(view);
    this.#window.setTitle(`VibeSys · ${hostLabel}`);
    view.webContents
      .loadURL(APP_ENTRY_URL)
      .catch(() => this.#deps.log('could not load the run view'));
    this.setMode('run');
    return {
      wake: () => {
        if (!view.webContents.isDestroyed()) view.webContents.send(WAKE_CHANNEL);
      },
      close: () => {
        if (this.#run === view) this.#closeRunPage();
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

  #closeRunPage(): void {
    const view = this.#run;
    if (view === null) return;
    this.#run = null;
    if (!this.#window.isDestroyed()) this.#window.contentView.removeChildView(view);
    view.webContents.close();
    this.setMode('welcome');
  }

  /** Lay the views out: the filling view, and the welcome view's strip over a run. */
  #layout(): void {
    if (this.#window.isDestroyed()) return;
    const {width, height} = this.#window.getContentBounds();
    const full = {x: 0, y: 0, width, height};
    // The run page follows one dark theme; the welcome view follows the system's.
    nativeTheme.themeSource = this.#mode === 'run' ? 'dark' : 'system';
    if (this.#run !== null) {
      this.#run.setBounds(full);
      this.#run.setVisible(this.#mode === 'run');
    }
    this.welcome.setBounds(this.#mode === 'run' ? {...full, height: TITLEBAR_HEIGHT} : full);
    // Re-adding moves the welcome view (the title strip) above the run view.
    this.#window.contentView.addChildView(this.welcome);
  }
}
