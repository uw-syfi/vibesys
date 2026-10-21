/**
 * Sandboxed preload of the app shell: the page's only way out of its sandbox. (Its name and the
 * `welcome` channel prefix are the surface's historical ones.)
 *
 * It exposes one function per request (`@vibesys/ipc`'s `WelcomeBridge`); each is an `invoke` the
 * main process answers only for a window's own shell document, and parses with `@vibesys/ipc`'s
 * `parseWelcomeRequest` (`welcome.test.ts` holds the two to the same arguments). Three events come
 * back: the recent runs (`onRecent`), a host's runs as its helper pushed them (`onListing`), and the
 * preferences (`onPreferences`). Before the page's scripts run it asks, synchronously, for what the first paint needs
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
  runTransportV2: 'vibesys:welcome:run-transport-v2',
  setCheckout: 'vibesys:welcome:set-checkout',
  cancelCheckout: 'vibesys:welcome:cancel-checkout',
  signIn: 'vibesys:welcome:sign-in',
  complete: 'vibesys:welcome:complete',
  logs: 'vibesys:welcome:logs',
  releaseLogs: 'vibesys:welcome:release-logs',
  tasks: 'vibesys:welcome:tasks',
  harnesses: 'vibesys:welcome:harnesses',
  attach: 'vibesys:welcome:attach',
  attachReader: 'vibesys:welcome:attach-reader',
  resume: 'vibesys:welcome:resume',
  releaseRun: 'vibesys:welcome:release-run',
  readEvents: 'vibesys:welcome:read-events',
  readTrace: 'vibesys:welcome:read-trace',
  readSummary: 'vibesys:welcome:read-summary',
  readSpan: 'vibesys:welcome:read-span',
  runDigests: 'vibesys:welcome:run-digests',
  projectRuns: 'vibesys:welcome:project-runs',
  usageWindow: 'vibesys:welcome:usage-window',
  preferences: 'vibesys:welcome:preferences',
  setAppearance: 'vibesys:welcome:set-appearance',
  setNotifications: 'vibesys:welcome:set-notifications',
  archive: 'vibesys:welcome:archive',
  machines: 'vibesys:welcome:machines',
  machine: 'vibesys:welcome:machine',
  inspectCheckout: 'vibesys:welcome:inspect-checkout',
  removeMachine: 'vibesys:welcome:remove-machine',
  compute: 'vibesys:welcome:compute',
  testCluster: 'vibesys:welcome:test-cluster',
  addCluster: 'vibesys:welcome:add-cluster',
  removeCluster: 'vibesys:welcome:remove-cluster',
  agentLogins: 'vibesys:welcome:agent-logins',
  startLogin: 'vibesys:welcome:start-login',
  describeMachine: 'vibesys:welcome:describe-machine',
  preflight: 'vibesys:welcome:preflight',
  examples: 'vibesys:welcome:examples',
  createFromExample: 'vibesys:welcome:create-from-example',
  copyOut: 'vibesys:welcome:copy-out',
  startRun: 'vibesys:welcome:start-run',
  helperFeatures: 'vibesys:welcome:helper-features',
  taskGoal: 'vibesys:welcome:task-goal',
  exampleDestination: 'vibesys:welcome:example-destination',
  openRunWindow: 'vibesys:welcome:open-run-window',
  showMainWindow: 'vibesys:welcome:show-main-window',
  openSettings: 'vibesys:welcome:open-settings',
  openExternal: 'vibesys:welcome:open-external',
};
const EVENTS: Events = {
  recent: 'vibesys:welcome:recent',
  listing: 'vibesys:welcome:listing',
  preferences: 'vibesys:welcome:preferences-changed',
};
const FIRST_PAINT_CHANNEL: typeof import('@vibesys/ipc').FIRST_PAINT_CHANNEL =
  'vibesys:welcome:first-paint';
const CONNECT_CHANNEL: typeof import('@vibesys/ipc').CONNECT_CHANNEL = 'vibesys:connect';
const WAKE_CHANNEL: typeof import('@vibesys/ipc').WAKE_CHANNEL = 'vibesys:wake';
const OPEN_SETTINGS_CHANNEL: typeof import('@vibesys/ipc').OPEN_SETTINGS_CHANNEL =
  'vibesys:open-settings';
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
    runTransportV2: (host, enabled) => call(CHANNELS.runTransportV2, host, enabled),
    overview: () => call(CHANNELS.overview),
    setCheckout: (host, path, change) => call(CHANNELS.setCheckout, host, path, change),
    cancelCheckout: change => call(CHANNELS.cancelCheckout, change),
    signIn: host => call(CHANNELS.signIn, host),
    complete: (host, directory) => call(CHANNELS.complete, host, directory),
    logs: (host, instance, request) => call(CHANNELS.logs, host, instance, request),
    releaseLogs: () => call(CHANNELS.releaseLogs),
    tasks: (host, project) => call(CHANNELS.tasks, host, project),
    harnesses: host => call(CHANNELS.harnesses, host),
    attach: (host, instance) => call(CHANNELS.attach, host, instance),
    attachReader: (host, project, runId) => call(CHANNELS.attachReader, host, project, runId),
    resume: (host, project, run) => call(CHANNELS.resume, host, project, run),
    releaseRun: () => call(CHANNELS.releaseRun),
    readEvents: (host, project, runId, fromSequence, max) =>
      call(CHANNELS.readEvents, host, project, runId, fromSequence, max),
    readTrace: (host, project, runId, fromSequence, max) =>
      call(CHANNELS.readTrace, host, project, runId, fromSequence, max),
    readSummary: (host, project, runId) => call(CHANNELS.readSummary, host, project, runId),
    readSpan: (host, project, runId, spanId, fromSequence, max) =>
      call(CHANNELS.readSpan, host, project, runId, spanId, fromSequence, max),
    runDigests: (host, project, runIds) => call(CHANNELS.runDigests, host, project, runIds),
    projectRuns: (host, project) => call(CHANNELS.projectRuns, host, project),
    usageWindow: (host, project, runIds, since, until) =>
      call(CHANNELS.usageWindow, host, project, runIds, since, until),
    connectRun: id => capabilities.connectRun(id),
    onWake: listener => capabilities.on(WAKE_CHANNEL, () => listener()),
    preferences: () => call(CHANNELS.preferences),
    setAppearance: appearance => call(CHANNELS.setAppearance, appearance),
    setNotifications: enabled => call(CHANNELS.setNotifications, enabled),
    archive: (run, archived) => call(CHANNELS.archive, run, archived),
    machines: () => call(CHANNELS.machines),
    machine: host => call(CHANNELS.machine, host),
    inspectCheckout: (host, path) => call(CHANNELS.inspectCheckout, host, path),
    removeMachine: host => call(CHANNELS.removeMachine, host),
    compute: host => call(CHANNELS.compute, host),
    testCluster: (host, cluster) => call(CHANNELS.testCluster, host, cluster),
    addCluster: (host, cluster) => call(CHANNELS.addCluster, host, cluster),
    removeCluster: (host, name) => call(CHANNELS.removeCluster, host, name),
    agentLogins: host => call(CHANNELS.agentLogins, host),
    startLogin: (host, provider) => call(CHANNELS.startLogin, host, provider),
    describeMachine: host => call(CHANNELS.describeMachine, host),
    preflight: (host, inputs) => call(CHANNELS.preflight, host, inputs),
    examples: host => call(CHANNELS.examples, host),
    createFromExample: (host, example, destination) =>
      call(CHANNELS.createFromExample, host, example, destination),
    copyOut: (host, source, destination) => call(CHANNELS.copyOut, host, source, destination),
    startRun: (host, inputs) => call(CHANNELS.startRun, host, inputs),
    helperFeatures: host => call(CHANNELS.helperFeatures, host),
    taskGoal: (host, project, task) => call(CHANNELS.taskGoal, host, project, task),
    exampleDestination: (host, example, destination) =>
      call(CHANNELS.exampleDestination, host, example, destination),
    openRunWindow: run => call(CHANNELS.openRunWindow, run),
    showMainWindow: () => call(CHANNELS.showMainWindow),
    openSettings: () => call(CHANNELS.openSettings),
    openExternal: url => call(CHANNELS.openExternal, url),
    onOpenSettings: listener => capabilities.on(OPEN_SETTINGS_CHANNEL, () => listener()),
    onRecent: on(EVENTS.recent),
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
  electron.contextBridge.exposeInMainWorld('vibesysGateway', {
    unary: (host, method, bytes) => {
      let cancel = (): void => {};
      const result = new Promise<Uint8Array>((resolve, reject) => {
        let response: Uint8Array | undefined;
        cancel = openGateway(
          'unary',
          host,
          method,
          bytes,
          value => {
            response = value;
          },
          error => {
            if (error !== undefined) reject(new Error(error));
            else if (response === undefined) reject(new Error('missing unary response'));
            else resolve(response);
          },
        );
      });
      return {result, cancel: () => cancel()};
    },
    stream: (host, method, bytes, receive, ended) => ({
      cancel: openGateway('stream', host, method, bytes, receive, ended),
    }),
  } satisfies import('@vibesys/ipc').GatewayBridge);
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

/** Ports are owned in the preload; renderer callbacks receive only structured protobuf bytes. */
function openGateway(
  kind: 'unary' | 'stream',
  host: string,
  method: string,
  bytes: Uint8Array,
  receive: (bytes: Uint8Array) => void,
  ended: (error?: string) => void,
): () => void {
  const electron: typeof import('electron') = require('electron');
  const {port1, port2} = new MessageChannel();
  let closed = false;
  const finish = (error?: string): void => {
    if (closed) return;
    closed = true;
    port1.postMessage({cancel: true});
    port1.close();
    ended(error);
  };
  port1.onmessage = event => {
    const reply = event.data as import('@vibesys/ipc').GatewayReply;
    if ('bytes' in reply) receive(reply.bytes);
    else finish(reply.error);
  };
  electron.ipcRenderer.postMessage('vibesys:gateway', {kind, host, method, bytes}, [port2]);
  return () => finish('cancelled');
}
