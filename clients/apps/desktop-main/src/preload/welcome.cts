/**
 * Sandboxed preload of the welcome view: the page's only way out of its sandbox.
 *
 * It exposes one function per welcome request (`@vibesys/ipc`'s `WelcomeBridge`); each is an
 * `invoke` the main process answers only for the welcome view's own web contents, and parses with
 * `@vibesys/ipc`'s `parseWelcomeRequest` (`welcome.test.ts` holds the two to the same arguments).
 * Five events come back: the window's state (`onChrome`), the recent runs (`onRecent`), a host to
 * show (`onShowHost`), a host's runs as its helper pushed them (`onListing`), and the preferences
 * (`onPreferences`). Before the page's scripts run it asks, synchronously, for what the first paint needs
 * (the stored theme and the window controls' inset) and sets it on the document. Sandboxed
 * preloads cannot be ES modules or import local files, hence `.cts` and the channel names repeated
 * here; the types keep them in step. It loads only Electron (the package-graph checker's
 * `preload-sandbox`): the payload types come from the bridge interface, which types every member
 * below.
 */
import type {FirstPaint, WelcomeBridge, WelcomeEvent, WelcomeResult} from '@vibesys/ipc';

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
  logs: 'vibesys:welcome:logs',
  releaseLogs: 'vibesys:welcome:release-logs',
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
  stopConfirmed: 'vibesys:welcome:stop-confirmed',
  releaseRun: 'vibesys:welcome:release-run',
  preferences: 'vibesys:welcome:preferences',
  setAppearance: 'vibesys:welcome:set-appearance',
  setNotifications: 'vibesys:welcome:set-notifications',
  archive: 'vibesys:welcome:archive',
  machines: 'vibesys:welcome:machines',
  machine: 'vibesys:welcome:machine',
  inspectCheckout: 'vibesys:welcome:inspect-checkout',
  removeMachine: 'vibesys:welcome:remove-machine',
  describeMachine: 'vibesys:welcome:describe-machine',
  preflight: 'vibesys:welcome:preflight',
  examples: 'vibesys:welcome:examples',
  createFromExample: 'vibesys:welcome:create-from-example',
  copyOut: 'vibesys:welcome:copy-out',
  startRun: 'vibesys:welcome:start-run',
  helperFeatures: 'vibesys:welcome:helper-features',
};
const EVENTS: Events = {
  chrome: 'vibesys:welcome:chrome',
  recent: 'vibesys:welcome:recent',
  showHost: 'vibesys:welcome:show-host',
  listing: 'vibesys:welcome:listing',
  preferences: 'vibesys:welcome:preferences-changed',
};
const FIRST_PAINT_CHANNEL: typeof import('@vibesys/ipc').FIRST_PAINT_CHANNEL =
  'vibesys:welcome:first-paint';
const CONNECT_CHANNEL: typeof import('@vibesys/ipc').CONNECT_CHANNEL = 'vibesys:connect';
const WAKE_CHANNEL: typeof import('@vibesys/ipc').WAKE_CHANNEL = 'vibesys:wake';
const RUN_PORT_MESSAGE: typeof import('@vibesys/ipc').RUN_PORT_MESSAGE = 'vibesys:run-port';

/** Effects the sandbox grants this preload, injectable so the exposed bridge has a public Fake. */
interface WelcomePreloadCapabilities {
  readonly platform: string;
  /** Ask the main process on `channel`; it answers with a `WelcomeResult`. */
  invoke(channel: string, ...args: unknown[]): Promise<unknown>;
  /** Hear every value the main process sends on `channel`. */
  on(channel: string, listener: (value: unknown) => void): void;
  /**
   * Open one run connection: hand one end of a new channel to the main process and the other to
   * the page, as a window message named `id`.
   */
  connectRun(id: string): void;
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
    inspect: (host, instance, inspection) => call(CHANNELS.inspect, host, instance, inspection),
    logs: (host, instance, request) => call(CHANNELS.logs, host, instance, request),
    releaseLogs: () => call(CHANNELS.releaseLogs),
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
    releaseRun: () => call(CHANNELS.releaseRun),
    connectRun: id => capabilities.connectRun(id),
    onWake: listener => capabilities.on(WAKE_CHANNEL, () => listener()),
    stopConfirmed: (host, instance, force) => call(CHANNELS.stopConfirmed, host, instance, force),
    preferences: () => call(CHANNELS.preferences),
    setAppearance: appearance => call(CHANNELS.setAppearance, appearance),
    setNotifications: enabled => call(CHANNELS.setNotifications, enabled),
    archive: (run, archived) => call(CHANNELS.archive, run, archived),
    machines: () => call(CHANNELS.machines),
    machine: host => call(CHANNELS.machine, host),
    inspectCheckout: (host, path) => call(CHANNELS.inspectCheckout, host, path),
    removeMachine: host => call(CHANNELS.removeMachine, host),
    describeMachine: host => call(CHANNELS.describeMachine, host),
    preflight: (host, inputs) => call(CHANNELS.preflight, host, inputs),
    examples: host => call(CHANNELS.examples, host),
    createFromExample: (host, example, destination) =>
      call(CHANNELS.createFromExample, host, example, destination),
    copyOut: (host, source, destination) => call(CHANNELS.copyOut, host, source, destination),
    startRun: (host, inputs) => call(CHANNELS.startRun, host, inputs),
    helperFeatures: host => call(CHANNELS.helperFeatures, host),
    onChrome: on(EVENTS.chrome),
    onRecent: on(EVENTS.recent),
    onShowHost: on(EVENTS.showHost),
    onListing: on(EVENTS.listing),
    onPreferences: on(EVENTS.preferences),
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
      connectRun: id => {
        const {port1: port, port2: remote} = new MessageChannel();
        electron.ipcRenderer.postMessage(CONNECT_CHANNEL, null, [remote]);
        // A port cannot cross the context bridge as a value; it can as this window's message.
        window.postMessage({type: RUN_PORT_MESSAGE, id}, '*', [port]);
      },
    }),
  );
  prepareFirstPaint(electron.ipcRenderer.sendSync(FIRST_PAINT_CHANNEL));
}

/**
 * Set the stored theme and the window controls' inset (`--chrome-inset-width`,
 * `--chrome-inset-height`) on the root element as the parser inserts it, before the first paint. A
 * preload runs before the document has a root element, so this waits for it with a mutation
 * observer, whose callback runs before the page renders.
 */
function prepareFirstPaint(reply: unknown): void {
  const firstPaint = reply as FirstPaint | undefined;
  if (firstPaint === undefined || firstPaint === null) return;
  const apply = (): boolean => {
    const root = document.documentElement;
    if (root === null) return false;
    root.setAttribute('data-theme', firstPaint.theme);
    root.style.setProperty('--chrome-inset-width', `${firstPaint.chromeInset.width}px`);
    root.style.setProperty('--chrome-inset-height', `${firstPaint.chromeInset.height}px`);
    return true;
  };
  if (apply()) return;
  const observer = new MutationObserver(() => {
    if (apply()) observer.disconnect();
  });
  observer.observe(document, {childList: true});
}

export = createWelcomePreload;
