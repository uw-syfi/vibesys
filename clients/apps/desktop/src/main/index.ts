/**
 * Electron main process entry (`node` runtime): the composition root, and the one place the main
 * process runs Effects. It builds the link pool, the gateway dialers and the gateway calls inside
 * one scope that lives as long as the app, serves the gateway to the window over one MessagePort
 * per call, and opens the one window on the built UI with the hosts to read in its address.
 * Quitting closes the scope, which ends every call, gateway session and ssh master.
 */
import {randomUUID} from 'node:crypto';
import {homedir, userInfo} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Layer from 'effect/Layer';
import * as Scope from 'effect/Scope';
import {app, BrowserWindow, ipcMain, session, type WebContents} from 'electron';
import {hostKey} from '../state/host.js';
import {controlDirectory} from './control-directory.js';
import {
  nodeFileLock,
  nodeListDirectory,
  nodeReadFile,
  nodeSweepStaged,
  nodeWriteFile,
} from './files.js';
import {installGatewayIpc, type RunCall} from './gateway.js';
import {NodeGatewayArtifacts} from './gateway-artifacts.js';
import {makeGatewayCalls} from './gateway-calls.js';
import {makeGatewayDialers} from './gateway-dialers.js';
import {makeLinkPool, RETIRE_BOUND_MS} from './link-pool.js';
import {NodeLocalHost} from './local-host.js';
import {installMachinesIpc, serveMachines, sshArgsFor} from './machines.js';
import {isOwnWindow, type PortEvent} from './port-ipc.js';
import {
  installPreferencesIpc,
  makePreferencesStore,
  type PreferencesStore,
  preferencesPath,
} from './preferences.js';
import {NodeProcessSpawner} from './process.js';
import {quit} from './quit.js';
import {makeSavedHostsStore, savedHostsPath} from './saved-hosts-store.js';
import {makeSshConfigSource, SSH_CONFIG_ENV, sshConfigArgs} from './ssh-config.js';
import {installViewerIpc, makeViewerStore} from './viewer.js';
import {chromeCss, MINIMUM_WINDOW, windowChrome, windowChromeOptions} from './window-chrome.js';
import {trackWindow} from './window-lifetime.js';
import {hostsFromArgv, isAppPage, startupHosts, windowUrl} from './window-policy.js';

/** `dist/renderer`: the built page, beside `dist/main`. */
const UI_DIRECTORY = fileURLToPath(new URL('../renderer/', import.meta.url));
const PRELOAD = fileURLToPath(new URL('../preload/index.cjs', import.meta.url));
/** The Go gateway binaries the build puts in `dist/gateway`. */
const GATEWAY_RESOURCES = fileURLToPath(new URL('../gateway/', import.meta.url));
/** Where the app keeps its files; tests point it at a scratch directory. */
const USER_DATA_ENV = 'VIBESYS_DESKTOP_USER_DATA';
/** How long a session started ahead of the first call waits for it: a slow window on a busy Mac. */
const LOCAL_GATEWAY_WARM = '15 seconds';
/**
 * How long quitting waits for the hosts to be released before exiting anyway. link-pool.ts's own
 * closing finalizer already gives up on any one frozen host's retirement past its own
 * `RETIRE_BOUND_MS` and lets it keep going in the background, so by the time this fires every
 * host's teardown has either finished or been let go: this is headroom for the rest of the scope's
 * cleanup after that (the gateway dialers and calls), not the normal way a slow host gets cut off.
 */
const RELEASE_BOUND_MS = RETIRE_BOUND_MS + 4_000;

const log = (message: string): void => {
  console.error(`vibesys-desktop: ${message}`);
};

/** Where the gateway's per-host logs go, with the host named. */
const hostLog = (host: string, line: string): void => log(`${host}: ${line}`);

/** The app's lifetime: everything the wiring opens ends when this closes. */
const lifetime = Scope.makeUnsafe();

/** The name of the person at this machine, or none when the system has no entry for the user. */
function userName(): string {
  try {
    return userInfo().username;
  } catch {
    return '';
  }
}

/** What the app needs from the wiring once it is built. */
async function wire(userData: string, preferences: PreferencesStore) {
  const wiring = Effect.gen(function* () {
    // Who this install is is stamped on every call to a host.
    const viewer = yield* makeViewerStore(preferences, {newId: randomUUID, userName}, log);
    const controlPath = join(yield* controlDirectory(process.getuid?.() ?? 0), '%C');
    // Left over from a save that was cut short: sweep the old staging files of the saved hosts.
    yield* nodeSweepStaged(savedHostsPath(userData)).pipe(
      Effect.catch(cause =>
        Effect.sync(() => log(`could not sweep stale saved-hosts files: ${cause.message}`)),
      ),
    );
    const hosts = makeSavedHostsStore(
      userData,
      {read: nodeReadFile, write: nodeWriteFile, lock: nodeFileLock},
      log,
    );
    const explicitConfig = process.env[SSH_CONFIG_ENV];
    const config = makeSshConfigSource(
      {read: nodeReadFile, list: nodeListDirectory},
      {config: explicitConfig ?? join(homedir(), '.ssh', 'config'), home: homedir()},
      log,
    );
    const pool = yield* makeLinkPool(
      controlPath,
      log,
      sshArgsFor(hosts, sshConfigArgs(explicitConfig)),
    );
    const dialers = yield* makeGatewayDialers(pool, hostLog);
    const calls = yield* makeGatewayCalls(dialers, hostLog, viewer.metadata);
    // This machine's gateway may have to be started, which takes 0.3 s, and the window needs about
    // as long to load before its first call: start it now so that the two run side by side.
    yield* calls.warm(hostKey({kind: 'local'}), LOCAL_GATEWAY_WARM);
    const saved = (yield* hosts.list).map(host => host.key);
    return {calls, saved, viewer, serveMachines: serveMachines(hosts, config)};
  }).pipe(
    Effect.provide(
      Layer.mergeAll(NodeProcessSpawner, NodeLocalHost, NodeGatewayArtifacts(GATEWAY_RESOURCES)),
    ),
    Effect.provideService(Scope.Scope, lifetime),
  );
  return Effect.runPromise(wiring);
}

const run: RunCall = (effect, signal) => Effect.runPromiseExit(effect, {signal});

/** Keep `contents` on the built page and refuse every new window and every network request. */
function confine(contents: WebContents): void {
  const stay = (event: {readonly url: string; preventDefault(): void}): void => {
    if (isAppPage(UI_DIRECTORY, event.url)) return;
    event.preventDefault();
    log('blocked navigation away from the app');
  };
  contents.on('will-navigate', stay);
  contents.on('will-redirect', stay);
  contents.setWindowOpenHandler(() => {
    log('blocked a new window');
    return {action: 'deny'};
  });
}

function lockDownSession(): void {
  const {defaultSession} = session;
  defaultSession.webRequest.onBeforeRequest(
    {urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*']},
    (_details, callback) => {
      log('blocked a network request');
      callback({cancel: true});
    },
  );
  defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  defaultSession.setPermissionCheckHandler(() => false);
}

/** Open the window on the built page, reading `hosts`. */
function openWindow(hosts: readonly string[]): BrowserWindow {
  const window = new BrowserWindow({
    width: 1280,
    height: 860,
    ...MINIMUM_WINDOW,
    title: 'VibeSys',
    show: false,
    ...windowChromeOptions(process.platform),
    webPreferences: {
      preload: PRELOAD,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  // The page keeps clear of what the window's own controls cover; full screen hides them.
  let inserted: string | undefined;
  const tell = async (): Promise<void> => {
    const key = await window.webContents.insertCSS(
      chromeCss(windowChrome(process.platform, window.isFullScreen())),
    );
    if (inserted !== undefined) await window.webContents.removeInsertedCSS(inserted);
    inserted = key;
  };
  window.webContents.on('did-finish-load', () => void tell());
  window.on('enter-full-screen', () => void tell());
  window.on('leave-full-screen', () => void tell());
  window.once('ready-to-show', () => window.show());
  void window.loadURL(windowUrl(UI_DIRECTORY, hosts));
  return window;
}

async function start(): Promise<void> {
  const userData = process.env[USER_DATA_ENV];
  if (userData !== undefined && userData !== '') app.setPath('userData', userData);
  const asked = hostsFromArgv(process.argv);
  await app.whenReady();
  lockDownSession();
  // A save the process died in the middle of left its staging file behind: sweep the old ones.
  await Effect.runPromise(
    nodeSweepStaged(preferencesPath(app.getPath('userData'))).pipe(
      Effect.catch(cause =>
        Effect.sync(() => log(`could not sweep stale preference files: ${cause.message}`)),
      ),
    ),
  );
  const preferences = makePreferencesStore(
    app.getPath('userData'),
    {read: nodeReadFile, write: nodeWriteFile, lock: nodeFileLock},
    log,
  );
  const {
    calls,
    saved,
    viewer,
    serveMachines: serve,
  } = await wire(app.getPath('userData'), preferences);
  const windows = new Set<number>();
  const own = {
    has: (id: number) => windows.has(id),
    isAppUrl: (url: string) => isAppPage(UI_DIRECTORY, url),
  };
  installGatewayIpc(ipcMain, {
    calls,
    run,
    admits: (event: PortEvent) => isOwnWindow(event, own),
  });
  installPreferencesIpc(ipcMain, {
    store: preferences,
    own,
    run: effect => Effect.runPromise(effect),
  });
  installMachinesIpc(ipcMain, {serve, own, run: effect => Effect.runPromise(effect)});
  installViewerIpc(ipcMain, {store: viewer, own, run: effect => Effect.runPromise(effect)});
  app.on('web-contents-created', (_event, contents) => confine(contents));
  const window = openWindow(startupHosts(asked, [...saved]));
  trackWindow(windows, window);
  app.on('window-all-closed', () => app.quit());
  let releasing = false;
  app.on('will-quit', event => {
    if (releasing) return;
    releasing = true;
    event.preventDefault();
    void quit({
      release: () => Effect.runPromise(Scope.close(lifetime, Exit.void)),
      deadline: () => {
        let timer: ReturnType<typeof setTimeout>;
        const elapsed = new Promise<void>(resolve => {
          timer = setTimeout(resolve, RELEASE_BOUND_MS);
        });
        return {elapsed, cancel: () => clearTimeout(timer)};
      },
      timedOut: () => log('gave up waiting for the hosts to be released'),
      exit: () => app.exit(0),
    });
  });
}

void start().catch(error => {
  log(error instanceof Error ? error.message : String(error));
  app.exit(1);
});
