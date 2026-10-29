/**
 * Real-Electron composition for a host whose VibeSys predates the registry. Production owns the
 * app shell page, its preload, the IPC binders, the hosts and machines features, the settings
 * store, the connection manager and the gateway bridge; the gateway is the shipping Go
 * executable. The only stand-in is the host's checkout, a directory whose `vibesys` answers as an
 * older checkout does (the spec writes it from the recorded argparse output).
 */
// First, before any page loads: see the module for the Playwright race it closes.
import './pin-performance-global.mjs';
import {spawn} from 'node:child_process';
import {join} from 'node:path';
import {
  ConnectionManager,
  fromChild,
  GatewayCalls,
  gatewayProcess,
  LocalLink,
  MUTATE_METHOD,
  READ_METHOD,
  registryClusters,
  registryReads,
  registrySavedRuns,
  registrySetup,
} from '@vibesys/connection';
import {Hosts} from '@vibesys/feature-hosts/main';
import {MachinesMain} from '@vibesys/feature-machines/main';
import {hostKey} from '@vibesys/hosts';
import {WELCOME_EVENTS} from '@vibesys/ipc';
import {SettingsStore, settingsFor, withHostSettings} from '@vibesys/settings';
import {app, ipcMain, webContents} from 'electron';
import {firstLoad} from './first-load.mjs';

const root = process.env['VIBESYS_GATEWAY_ROOT'];
const binary = process.env['VIBESYS_GATEWAY_BINARY'];
const checkout = process.env['VIBESYS_OLDER_CHECKOUT'];
if (!root || !binary || !checkout) throw new Error('older host fixture configuration is missing');

const [
  {confineContents, lockDownSession, registerAppScheme, serveBundledUi},
  {Desktop},
  {WelcomeFeed},
  {rendererGeneration},
  {AppWindow},
  {chromeInset},
  {realTimers},
] = await Promise.all([
  import(new URL('../dist/main/app-security.js', import.meta.url)),
  import(new URL('../dist/main/desktop.js', import.meta.url)),
  import(new URL('../dist/main/welcome-feed.js', import.meta.url)),
  import(new URL('../dist/main/welcome-ipc.js', import.meta.url)),
  import(new URL('../dist/main/window.js', import.meta.url)),
  import(new URL('../dist/main/window-chrome.js', import.meta.url)),
  import(new URL('../dist/main/app-lifecycle.js', import.meta.url)),
]);

const SHELL_URL = 'app://vibesys/shell.html';
const LOCAL = {kind: 'local'};
const logs = [];
const log = message => logs.push(message);

ipcMain.on('vibesys:welcome:first-paint', event => {
  event.returnValue = {theme: 'paper', chromeInset: chromeInset(process.platform)};
});
registerAppScheme();
/** First loads of the pages created while the app starts (the app shell). */
const startupLoads = [];
app.on('web-contents-created', (_event, contents) => {
  confineContents(contents, log);
  startupLoads.push(firstLoad(contents));
});
app.on('window-all-closed', () => app.quit());
let appWindow = null;

const started = app.whenReady().then(async () => {
  lockDownSession(log);
  serveBundledUi();

  const settings = new SettingsStore({
    settingsPath: join(root, 'settings', 'hosts.json'),
    recentPath: join(root, 'settings', 'recent.json'),
    preferencesPath: join(root, 'settings', 'preferences.json'),
    log,
  });
  await settings.updateHosts(current =>
    withHostSettings(current, LOCAL, {...settingsFor(current, LOCAL), checkout}),
  );
  const manager = new ConnectionManager({
    linkFor: () => new LocalLink(),
    appVersion: app.getVersion(),
    scheduleTimeout: realTimers,
    log,
  });
  const calls = new GatewayCalls(
    () => ({
      dial: async () => gatewayProcess(fromChild(spawn(binary, ['connect', '--root', root], {stdio: 'pipe'})), realTimers),
    }),
    realTimers,
    (host, diagnostic) => log(`${host} answered a failed call: ${diagnostic}`),
  );
  manager.onDisposing(() => calls.close());
  app.on('before-quit', () => calls.close());

  const welcome = new WelcomeFeed();
  appWindow = new AppWindow({
    log,
    surfaceUrl: SHELL_URL,
    connect: port => port.close(),
    closed: () => {},
    fatal: error => log(error.message),
  });
  const setup = registrySetup({unary: (id, bytes, signal) => calls.unary(hostKey(id), READ_METHOD, bytes, signal)});
  const hosts = new Hosts({
    manager,
    settings,
    localCheckout: checkout,
    sshConfigPath: join(root, 'ssh_config'),
    setup,
    runs: {start() {}, cancel() {}},
    savedRuns: registrySavedRuns({
      checkout: id => hosts.checkout(id),
      unary: (id, bytes, signal) => calls.unary(hostKey(id), READ_METHOD, bytes, signal),
    }),
    log,
    push: listing => appWindow.sendWelcome(WELCOME_EVENTS.listing, welcome.listing(listing)),
    retire: host => welcome.retireHost(host),
  });
  const machines = new MachinesMain({
    settings,
    removeHost: id => hosts.remove(id),
    checkout: id => hosts.checkout(id),
    connection: id => hosts.connection(id),
    registry: registryReads({
      checkout: id => hosts.checkout(id),
      unary: (id, bytes, signal) => calls.unary(hostKey(id), READ_METHOD, bytes, signal),
    }),
    clusters: registryClusters({
      checkout: id => hosts.checkout(id),
      unary: (id, bytes, signal) => calls.unary(hostKey(id), MUTATE_METHOD, bytes, signal),
    }),
    setup,
    runs: {live: async () => 0},
  });
  const runs = {connect: port => port.close(), detach() {}};
  new Desktop({
    window: appWindow,
    welcome,
    hosts,
    history: {read: async () => ({kind: 'unsupported'})},
    machines,
    runs,
    settings,
    preferences: {
      answer: async () => ({
        appearance: {mode: 'system', light: 'paper', dark: 'ink'},
        notifications: {enabled: true},
        archived: [],
      }),
    },
    newRun: {},
    gatewaySetting: async () => true,
    runWindows: {open: () => ({}), sendWelcome: () => undefined},
    shellUrl: SHELL_URL,
    welcomeIpc: {
      handle: (channel, listener) =>
        ipcMain.handle(channel, (event, ...args) =>
          listener(
            {
              senderId: event.sender.id,
              senderUrl: event.senderFrame?.url ?? null,
              rendererGeneration: rendererGeneration(event.senderFrame),
            },
            ...args,
          ),
        ),
    },
    log,
  });
});

function shell() {
  const id = appWindow?.welcome.currentId();
  const contents = id === null || id === undefined ? null : webContents.fromId(id);
  if (!contents || contents.isDestroyed()) throw new Error('the app shell is unavailable');
  return contents;
}

/** The control surface the spec drives: it reads and presses the shell page as a person does. */
globalThis.vibesysOlderHostE2E = {
  /** The main-process log: where the host's own words go. */
  logs: () => [...logs],
  /** Resolves once the app shell has finished its first load. */
  async loaded() {
    await started;
    await Promise.all(startupLoads);
    shell();
  },
  /** Run `script` in the shell page. */
  evaluate: script => shell().executeJavaScript(script, true),
};
