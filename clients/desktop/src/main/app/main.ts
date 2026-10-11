/**
 * Electron main process: the composition root. It reads what the app was asked to open, takes the
 * single-instance lock, and wires the lifecycle (`app-lifecycle.ts`), the page confinement
 * (`app-security.ts`), the settings store, the connection manager, the features, and the window.
 */
import {homedir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {app, Menu, net, powerMonitor} from 'electron';
import type {HostId} from '../../common/hosts.js';
import {WELCOME_EVENTS} from '../../common/welcome-protocol.js';
import {Hosts} from '../features/hosts/index.js';
import {RunControl} from '../features/run-control/index.js';
import {
  ConnectionManager,
  controlDirectory,
  installAskpass,
  type Link,
  LocalLink,
  SshLink,
} from '../platform/connection/index.js';
import {remember, SettingsStore} from '../platform/settings/index.js';
import {AppLifecycle, realTimers} from './app-lifecycle.js';
import {
  confineContents,
  lockDownSession,
  registerAppScheme,
  serveBundledUi,
} from './app-security.js';
import {Desktop} from './desktop.js';
import {type LaunchPlan, parseLaunch} from './launch-args.js';
import {AppWindow} from './window.js';

/** The repository checkout this app was built in: This Mac's VibeSys checkout by default. */
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../../', import.meta.url)).replace(
  /\/+$/,
  '',
);
/** How long quitting waits for the hosts to be released before exiting anyway. */
const RELEASE_BOUND_MS = 5_000;
/** How often the main process looks for the network coming back. */
const NETWORK_POLL_MS = 5_000;

function log(message: string): void {
  console.error(`vibesys-desktop: ${message}`);
}

/** The launch plan in `argv` (a process's own command line). */
function planFrom(argv: readonly string[], cwd: string): LaunchPlan {
  return parseLaunch(argv.slice(app.isPackaged ? 1 : 2), cwd);
}

function applicationMenu(): Menu {
  const first: Electron.MenuItemConstructorOptions =
    process.platform === 'darwin' ? {role: 'appMenu'} : {role: 'fileMenu'};
  return Menu.buildFromTemplate([
    first,
    {role: 'editMenu'},
    {role: 'viewMenu'},
    {role: 'windowMenu'},
  ]);
}

/** Report the network coming back (offline to online) to every host connection. */
function watchNetwork(manager: ConnectionManager): void {
  let online = true;
  const poll = (): void => {
    const now = net.isOnline();
    if (now && !online) manager.wake();
    online = now;
    realTimers(poll, NETWORK_POLL_MS);
  };
  realTimers(poll, NETWORK_POLL_MS);
}

async function openWindow(manager: ConnectionManager): Promise<Desktop> {
  lockDownSession(log);
  serveBundledUi();
  Menu.setApplicationMenu(applicationMenu());
  const userData = app.getPath('userData');
  const settings = new SettingsStore({
    settingsPath: join(userData, 'hosts.json'),
    recentPath: join(userData, 'recent.json'),
    log,
  });
  let desktop: Desktop | null = null;
  const changed = (): void => desktop?.chromeChanged();
  const hosts = new Hosts({
    manager,
    settings,
    localCheckout: REPOSITORY_ROOT,
    sshConfigPath: join(homedir(), '.ssh', 'config'),
    push: listing => window.send(WELCOME_EVENTS.listing, listing),
  });
  const runs: RunControl = new RunControl({
    connection: id => hosts.connection(id),
    openPage: label => window.openRunPage(label),
    confirm: text => window.confirmStop(text),
    remember: run => settings.updateRecent(current => remember(current, run)),
    changed,
    log,
    now: () => Date.now(),
  });
  const window = new AppWindow({
    log,
    connect: port => runs.connect(port),
    closed: () => runs.detach(),
  });
  desktop = new Desktop({window, hosts, runs, settings, log});
  // A machine that slept has dead connections nobody has noticed yet: check them now.
  powerMonitor.on('resume', () => manager.wake());
  watchNetwork(manager);
  return desktop;
}

async function connectionManager(): Promise<ConnectionManager> {
  const controlPath = join(await controlDirectory(process.getuid?.() ?? 0), '%C');
  const askpass = await installAskpass(join(app.getPath('userData'), 'askpass.sh'));
  return new ConnectionManager({
    linkFor: (host: HostId): Link =>
      host.kind === 'local'
        ? new LocalLink()
        : new SshLink({alias: host.alias, controlPath, askpass}),
    appVersion: app.getVersion(),
    scheduleTimeout: realTimers,
    log,
  });
}

function main(): void {
  let plan: LaunchPlan;
  try {
    plan = planFrom(process.argv, process.env['INIT_CWD'] ?? '.');
  } catch (error) {
    log((error as Error).message);
    process.exit(2);
  }
  registerAppScheme();
  let manager: ConnectionManager | null = null;
  let desktop: Desktop | null = null;
  const lifecycle = new AppLifecycle({
    // Runs are detached and keep going; quitting ends this app's channels and ssh masters only.
    release: async () => manager?.[Symbol.asyncDispose](),
    // The app's one exit call site.
    exit: code => app.exit(code),
    scheduleTimeout: realTimers,
    releaseBoundMs: RELEASE_BOUND_MS,
  });
  // One instance per user-data directory, the settings files' only writer: a second launch hands
  // its command line to this one (`second-instance`) and exits.
  if (!app.requestSingleInstanceLock()) {
    log('VibeSys is already running; showing its window');
    lifecycle.quit(0);
    return;
  }
  app.on('second-instance', (_event, argv, cwd) => {
    if (desktop === null) return;
    desktop.focus();
    let second: LaunchPlan;
    try {
      second = planFrom(argv, cwd);
    } catch (error) {
      log(`ignoring a second launch's arguments: ${(error as Error).message}`);
      return;
    }
    desktop.open(second).catch((error: Error) => log(error.message));
  });
  app.on('will-quit', event => lifecycle.willQuit(event));
  for (const signal of ['SIGINT', 'SIGTERM'] as const) process.once(signal, () => app.quit());
  app.on('web-contents-created', (_event, contents) => confineContents(contents, log));
  app.on('window-all-closed', () => app.quit());
  void app.whenReady().then(async () => {
    manager = await connectionManager();
    desktop = await openWindow(manager);
    try {
      await desktop.open(plan);
    } catch (error) {
      log((error as Error).message);
      lifecycle.quit(1);
    }
  });
}

main();
