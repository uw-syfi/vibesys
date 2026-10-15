/**
 * Sandboxed preload of the welcome view: the page's only way out of its sandbox.
 *
 * It exposes one function per welcome request (`@vibesys/ipc`'s `WelcomeBridge`); each is an
 * `invoke` the main process answers only for the welcome view's own web contents, and parses with
 * `@vibesys/ipc`'s `parseWelcomeRequest` (`welcome.test.ts` holds the two to the same arguments).
 * Four events come back: the window's state (`onChrome`), the recent runs (`onRecent`), a host to
 * show (`onShowHost`), and a host's runs as its helper pushed them (`onListing`). Sandboxed
 * preloads cannot be ES modules or import local files, hence `.cts` and the channel names repeated
 * here; the types keep them in step. It loads only Electron (the package-graph checker's
 * `preload-sandbox`): the payload types come from the bridge interface, which types every member
 * below.
 */
import type {WelcomeBridge, WelcomeEvent, WelcomeResult} from '@vibesys/ipc';

type Channels = typeof import('@vibesys/ipc').WELCOME_CHANNELS;
type Events = typeof import('@vibesys/ipc').WELCOME_EVENTS;

const CHANNELS: Channels = {
  overview: 'vibesys:welcome:overview',
  hosts: 'vibesys:welcome:hosts',
  host: 'vibesys:welcome:host',
  setCheckout: 'vibesys:welcome:set-checkout',
  cancelCheckout: 'vibesys:welcome:cancel-checkout',
  cancelInspection: 'vibesys:welcome:cancel-inspection',
  signIn: 'vibesys:welcome:sign-in',
  complete: 'vibesys:welcome:complete',
  inspect: 'vibesys:welcome:inspect',
  tasks: 'vibesys:welcome:tasks',
  harnesses: 'vibesys:welcome:harnesses',
  start: 'vibesys:welcome:start',
  cancelStart: 'vibesys:welcome:cancel-start',
  attach: 'vibesys:welcome:attach',
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

/** Effects the sandbox grants this preload, injectable so the exposed bridge has a public Fake. */
interface WelcomePreloadCapabilities {
  readonly platform: string;
  /** Ask the main process on `channel`; it answers with a `WelcomeResult`. */
  invoke(channel: string, ...args: unknown[]): Promise<unknown>;
  /** Hear every value the main process sends on `channel`. */
  on(channel: string, listener: (value: unknown) => void): void;
}

/** Build the page capability from its Electron boundary effects. */
function createWelcomePreload(capabilities: WelcomePreloadCapabilities): WelcomeBridge {
  function call<T>(channel: string, ...args: unknown[]): Promise<WelcomeResult<T>> {
    return capabilities.invoke(channel, ...args) as Promise<WelcomeResult<T>>;
  }
  function on<T>(channel: string): (listener: (event: WelcomeEvent<T>) => void) => void {
    return listener => capabilities.on(channel, value => listener(value as WelcomeEvent<T>));
  }
  return {
    platform: capabilities.platform,
    overview: () => call(CHANNELS.overview),
    hosts: () => call(CHANNELS.hosts),
    host: host => call(CHANNELS.host, host),
    setCheckout: (host, path, change) => call(CHANNELS.setCheckout, host, path, change),
    cancelCheckout: change => call(CHANNELS.cancelCheckout, change),
    cancelInspection: inspection => call(CHANNELS.cancelInspection, inspection),
    signIn: host => call(CHANNELS.signIn, host),
    complete: (host, directory) => call(CHANNELS.complete, host, directory),
    inspect: (host, instance, includeLog, inspection) =>
      call(CHANNELS.inspect, host, instance, includeLog, inspection),
    tasks: (host, project) => call(CHANNELS.tasks, host, project),
    harnesses: host => call(CHANNELS.harnesses, host),
    start: (host, project, task, args, harness, start) =>
      call(CHANNELS.start, host, project, task, args, harness, start),
    cancelStart: start => call(CHANNELS.cancelStart, start),
    attach: (host, instance) => call(CHANNELS.attach, host, instance),
    resume: (host, project, run) => call(CHANNELS.resume, host, project, run),
    showRun: () => call(CHANNELS.showRun),
    showWelcome: () => call(CHANNELS.showWelcome),
    retry: () => call(CHANNELS.retry),
    stop: (host, instance, force) => call(CHANNELS.stop, host, instance, force),
    onChrome: on(EVENTS.chrome),
    onRecent: on(EVENTS.recent),
    onShowHost: on(EVENTS.showHost),
    onListing: on(EVENTS.listing),
  };
}

// Electron's sandboxed CommonJS preload cannot import local runtime modules, so production wiring
// remains in this file. Bun tests have no Electron renderer process and exercise the factory above.
if (process.type === 'renderer') {
  const electron: typeof import('electron') = require('electron');
  electron.contextBridge.exposeInMainWorld(
    'vibesysWelcome',
    createWelcomePreload({
      platform: process.platform,
      invoke: (channel, ...args) => electron.ipcRenderer.invoke(channel, ...args),
      on: (channel, listener) =>
        electron.ipcRenderer.on(channel, (_event, value) => listener(value)),
    }),
  );
}

export = createWelcomePreload;
