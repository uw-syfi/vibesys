/**
 * Sandboxed preload of the welcome view: the page's only way out of its sandbox.
 *
 * It exposes one function per welcome request (`welcome-protocol.ts`); each is an `invoke` the main
 * process answers only for the welcome view's own web contents. `onChrome` delivers the window's
 * state (which view fills it, the attached run's status). Sandboxed preloads cannot import local
 * files, hence the channel names repeated here; the types keep them in step.
 */
import electron = require('electron');
import type {
  ChromeState,
  WelcomeHost,
  WelcomeOverview,
  WelcomeRecentStatus,
  WelcomeResult,
} from './welcome-protocol.js';
import type {HostKey} from './host-settings.js';

type Channels = typeof import('./welcome-protocol.js').WELCOME_CHANNELS;

const CHANNELS: Channels = {
  overview: 'vibesys:welcome:overview',
  recentStatus: 'vibesys:welcome:recent-status',
  hosts: 'vibesys:welcome:hosts',
  host: 'vibesys:welcome:host',
  setCheckout: 'vibesys:welcome:set-checkout',
  signIn: 'vibesys:welcome:sign-in',
  tasks: 'vibesys:welcome:tasks',
  attach: 'vibesys:welcome:attach',
  start: 'vibesys:welcome:start',
  resume: 'vibesys:welcome:resume',
  showRun: 'vibesys:welcome:show-run',
  showWelcome: 'vibesys:welcome:show-welcome',
  retry: 'vibesys:welcome:retry',
  stop: 'vibesys:welcome:stop',
};
const CHROME: typeof import('./welcome-protocol.js').CHROME_CHANNEL = 'vibesys:welcome:chrome';

function call<T>(channel: string, ...args: unknown[]): Promise<WelcomeResult<T>> {
  return electron.ipcRenderer.invoke(channel, ...args) as Promise<WelcomeResult<T>>;
}

electron.contextBridge.exposeInMainWorld('vibesysWelcome', {
  platform: process.platform,
  overview: () => call<WelcomeOverview>(CHANNELS.overview),
  recentStatus: (host: HostKey) =>
    call<Record<string, WelcomeRecentStatus>>(CHANNELS.recentStatus, host),
  hosts: () => call<string[]>(CHANNELS.hosts),
  host: (host: HostKey) => call<WelcomeHost>(CHANNELS.host, host),
  setCheckout: (host: HostKey, path: string) => call<string>(CHANNELS.setCheckout, host, path),
  signIn: (host: HostKey) => call<null>(CHANNELS.signIn, host),
  tasks: (host: HostKey, project: string) => call<string[]>(CHANNELS.tasks, host, project),
  attach: (host: HostKey, instance: string) => call<null>(CHANNELS.attach, host, instance),
  start: (host: HostKey, project: string, task: string, args: string) =>
    call<null>(CHANNELS.start, host, project, task, args),
  resume: (host: HostKey, project: string, run: string) =>
    call<null>(CHANNELS.resume, host, project, run),
  showRun: () => call<null>(CHANNELS.showRun),
  showWelcome: () => call<null>(CHANNELS.showWelcome),
  retry: () => call<null>(CHANNELS.retry),
  stop: (host: HostKey, instance: string, force: boolean) =>
    call<null>(CHANNELS.stop, host, instance, force),
  onChrome: (listener: (state: ChromeState) => void) => {
    electron.ipcRenderer.on(CHROME, (_event, state: ChromeState) => listener(state));
  },
});
