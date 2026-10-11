/**
 * Sandboxed preload of the welcome view: the page's only way out of its sandbox.
 *
 * It exposes one function per welcome request (`@vibesys/ipc`'s `WelcomeBridge`); each is an
 * `invoke` the main process answers only for the welcome view's own web contents, and validates.
 * Four events come back: the window's state (`onChrome`), the recent runs (`onRecent`), a host to
 * show (`onShowHost`), and a host's runs as its helper pushed them (`onListing`). Sandboxed
 * preloads cannot import local files, hence the channel names repeated here; the types keep them
 * in step. It imports only `@vibesys/ipc` and Electron (`.dependency-cruiser.mjs`): the payload
 * types come from the bridge interface, which types every member below.
 */
import electron = require('electron');
import type {WelcomeBridge, WelcomeEvent, WelcomeResult} from '@vibesys/ipc';

type Channels = typeof import('@vibesys/ipc').WELCOME_CHANNELS;
type Events = typeof import('@vibesys/ipc').WELCOME_EVENTS;

const CHANNELS: Channels = {
  overview: 'vibesys:welcome:overview',
  hosts: 'vibesys:welcome:hosts',
  host: 'vibesys:welcome:host',
  setCheckout: 'vibesys:welcome:set-checkout',
  cancelCheckout: 'vibesys:welcome:cancel-checkout',
  signIn: 'vibesys:welcome:sign-in',
  tasks: 'vibesys:welcome:tasks',
  complete: 'vibesys:welcome:complete',
  attach: 'vibesys:welcome:attach',
  start: 'vibesys:welcome:start',
  cancelStart: 'vibesys:welcome:cancel-start',
  resume: 'vibesys:welcome:resume',
  showRun: 'vibesys:welcome:show-run',
  showWelcome: 'vibesys:welcome:show-welcome',
  retry: 'vibesys:welcome:retry',
  stop: 'vibesys:welcome:stop',
};
const EVENTS: Events = {
  chrome: 'vibesys:welcome:chrome',
  recent: 'vibesys:welcome:recent',
  showHost: 'vibesys:welcome:show-host',
  listing: 'vibesys:welcome:listing',
};

function call<T>(channel: string, ...args: unknown[]): Promise<WelcomeResult<T>> {
  return electron.ipcRenderer.invoke(channel, ...args) as Promise<WelcomeResult<T>>;
}

function on<T>(channel: string, listener: (event: WelcomeEvent<T>) => void): void {
  electron.ipcRenderer.on(channel, (_event, event: WelcomeEvent<T>) => listener(event));
}

const bridge: WelcomeBridge = {
  platform: process.platform,
  overview: () => call(CHANNELS.overview),
  hosts: () => call(CHANNELS.hosts),
  host: host => call(CHANNELS.host, host),
  setCheckout: (host, path, change) => call(CHANNELS.setCheckout, host, path, change),
  cancelCheckout: change => call(CHANNELS.cancelCheckout, change),
  signIn: host => call(CHANNELS.signIn, host),
  tasks: (host, project) => call(CHANNELS.tasks, host, project),
  complete: (host, directory) => call(CHANNELS.complete, host, directory),
  attach: (host, instance) => call(CHANNELS.attach, host, instance),
  start: (host, project, task, args, start) =>
    call(CHANNELS.start, host, project, task, args, start),
  cancelStart: start => call(CHANNELS.cancelStart, start),
  resume: (host, project, run) => call(CHANNELS.resume, host, project, run),
  showRun: () => call(CHANNELS.showRun),
  showWelcome: () => call(CHANNELS.showWelcome),
  retry: () => call(CHANNELS.retry),
  stop: (host, instance, force) => call(CHANNELS.stop, host, instance, force),
  onChrome: listener => on(EVENTS.chrome, listener),
  onRecent: listener => on(EVENTS.recent, listener),
  onShowHost: listener => on(EVENTS.showHost, listener),
  onListing: listener => on(EVENTS.listing, listener),
};

electron.contextBridge.exposeInMainWorld('vibesysWelcome', bridge);
