/**
 * Electron main process: the composition root. It reads what the app was asked to open, takes the
 * single-instance lock, and wires the lifecycle (`app-lifecycle.ts`), the page confinement
 * (`app-security.ts`), the settings store, the connection manager, the features, and the window.
 */
import {homedir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {
  ConnectionManager,
  controlDirectory,
  installAskpass,
  type Link,
  LocalLink,
  SshLink,
} from '@vibesys/connection';
import {Hosts} from '@vibesys/feature-hosts/main';
import {RunControl} from '@vibesys/feature-run-control/main';
import type {HostId} from '@vibesys/hosts';
import {WELCOME_EVENTS} from '@vibesys/ipc';
import {remember, SettingsStore} from '@vibesys/settings';
import {app, Menu, net, powerMonitor} from 'electron';
import {AppLaunches} from './app-launches.js';
import {AppLifecycle, realTimers} from './app-lifecycle.js';
import {
  confineContents,
  lockDownSession,
  registerAppScheme,
  serveBundledUi,
} from './app-security.js';
import {AppStartup} from './app-startup.js';
import {Desktop} from './desktop.js';
import {type LaunchPlan, parseLaunch} from './launch-args.js';
import {WelcomeFeed} from './welcome-feed.js';
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
  const welcome = new WelcomeFeed();
  const changed = (): void => desktop?.chromeChanged();
  const hosts = new Hosts({
    manager,
    settings,
    localCheckout: REPOSITORY_ROOT,
    sshConfigPath: join(homedir(), '.ssh', 'config'),
    log,
    push: listing => window.sendWelcome(WELCOME_EVENTS.listing, welcome.listing(listing)),
    retire: host => welcome.retireHost(host),
  });
  const runs: RunControl = new RunControl({
    connection: id => hosts.connection(id),
    openPage: label => window.openRunPage(label),
    confirm: text => window.confirmStop(text),
    remember: (run, current) => settings.updateRecent(recent => remember(recent, run), current),
    changed,
    log,
    now: () => Date.now(),
  });
  const window = new AppWindow({
    log,
    connect: (port, role) => runs.connect(port, role),
    closed: () => {
      runs.detach();
      changed();
    },
  });
  desktop = new Desktop({window, welcome, hosts, runs, settings, log});
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
        : new SshLink({alias: host.alias, controlPath, askpass, scheduleTimeout: realTimers}),
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
  const launches = new AppLaunches<LaunchPlan>(error => log(error.message));
  const startup = new AppStartup<LaunchPlan, ConnectionManager, Desktop>({
    ready: () => app.whenReady(),
    connectionManager,
    openWindow,
    openInitial: (desktop, initial) => launches.ready(desktop, initial),
    log,
  });
  const lifecycle = new AppLifecycle({
    // Runs are detached and keep going; quitting ends this app's channels and ssh masters only.
    release: () => startup.release(),
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
    let second: LaunchPlan;
    try {
      second = planFrom(argv, cwd);
    } catch (error) {
      log(`ignoring a second launch's arguments: ${(error as Error).message}`);
      return;
    }
    launches.second(second);
  });
  app.on('will-quit', event => lifecycle.willQuit(event));
  for (const signal of ['SIGINT', 'SIGTERM'] as const) process.once(signal, () => app.quit());
  app.on('web-contents-created', (_event, contents) => confineContents(contents, log));
  app.on('window-all-closed', () => app.quit());
  void startup.start(plan, lifecycle);
}

main();
