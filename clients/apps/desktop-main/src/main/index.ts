/**
 * Electron main process: the composition root. It reads what the app was asked to open, takes the
 * single-instance lock, and wires the lifecycle (`app-lifecycle.ts`), the page confinement
 * (`app-security.ts`), the settings store, the connection manager, the features, and the window.
 */
import {homedir} from 'node:os';
import {join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {
  ConnectionManager,
  controlDirectory,
  installAskpass,
  type Link,
  LocalLink,
  SshLink,
} from '@vibesys/connection';
import {RunNotifier} from '@vibesys/feature-app-shell/main';
import {Hosts} from '@vibesys/feature-hosts/main';
import {MachinesMain} from '@vibesys/feature-machines/main';
import {NewRunMain} from '@vibesys/feature-new-run/main';
import {type RelayPort, RunControl} from '@vibesys/feature-run-control/main';
import {PreferencesMain} from '@vibesys/feature-settings/main';
import type {HostId} from '@vibesys/hosts';
import {
  FIRST_PAINT_CHANNEL,
  type FirstPaint,
  type RunLogsRequest,
  WELCOME_EVENTS,
  type WelcomeEvent,
} from '@vibesys/ipc';
import type {RunRef} from '@vibesys/runs';
import {remember, SettingsStore} from '@vibesys/settings';
import {app, ipcMain, Menu, Notification, nativeTheme, net, powerMonitor} from 'electron';
import {SHELL_URL, surfaceUrl} from './app-assets.js';
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
import type {LaunchPlan} from './launch-args.js';
import {planFromSecondInstance, prepareLaunchHandoff} from './launch-handoff.js';
import {RunWindows} from './run-windows.js';
import {WelcomeFeed} from './welcome-feed.js';
import {rendererGeneration} from './welcome-ipc.js';
import {AppWindow} from './window.js';
import {chromeInset} from './window-chrome.js';

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

/** New run's requests, answered over the hosts' connections, recording each start in Recent. */
function newRunMain(hosts: Hosts, settings: SettingsStore): NewRunMain {
  return new NewRunMain({
    connect: id => hosts.connection(id),
    remember: run => settings.updateRecent(recent => remember(recent, run)),
    now: () => Date.now(),
  });
}

async function openWindow(
  manager: ConnectionManager,
  fatal: (error: Error) => void,
): Promise<Desktop> {
  lockDownSession(log);
  serveBundledUi();
  Menu.setApplicationMenu(applicationMenu());
  const userData = app.getPath('userData');
  const settings = new SettingsStore({
    settingsPath: join(userData, 'hosts.json'),
    recentPath: join(userData, 'recent.json'),
    preferencesPath: join(userData, 'preferences.json'),
    log,
  });
  let desktop: Desktop | null = null;
  const welcome = new WelcomeFeed();
  const surface = surfaceUrl(process.env['VIBESYS_DESKTOP_SHELL']);
  const preferences = new PreferencesMain({
    store: settings,
    nativeTheme,
    push: value => windows.broadcast(WELCOME_EVENTS.preferences, welcome.preferences(value)),
  });
  await preferences.load();
  // The preload asks for this before the page's scripts run, so the first paint is in the theme
  // and clear of the window controls.
  ipcMain.on(FIRST_PAINT_CHANNEL, event => {
    const firstPaint: FirstPaint = {
      theme: preferences.theme(),
      chromeInset: chromeInset(process.platform),
    };
    event.returnValue = firstPaint;
  });
  const notifier = new RunNotifier({
    enabled: () => preferences.notificationsEnabled(),
    notify: notice => {
      const notification = new Notification({title: notice.title, body: notice.body});
      notification.on('click', () => windows.main.focus());
      notification.show();
    },
  });
  const changed = (): void => desktop?.chromeChanged();
  const hosts = new Hosts({
    manager,
    settings,
    localCheckout: REPOSITORY_ROOT,
    sshConfigPath: join(homedir(), '.ssh', 'config'),
    log,
    push: listing => {
      notifier.listing(listing);
      windows.broadcast(WELCOME_EVENTS.listing, welcome.listing(listing));
    },
    retire: host => welcome.retireHost(host),
  });
  const runs: RunControl = new RunControl({
    connection: id => hosts.connection(id),
    // A run shown in a window of its own is live there; any other run in the main window.
    openPage: (run, title) => (windows.runs.showing(run) ?? windows.main).openRunPage(run, title),
    confirm: text => windows.main.confirmStop(text),
    remember: (run, current) => settings.updateRecent(recent => remember(recent, run), current),
    changed,
    log,
    now: () => Date.now(),
  });
  const windows = appWindows(runs, changed, surface, fatal);
  desktop = new Desktop({
    window: windows.main,
    welcome,
    hosts,
    machines: machinesOver(settings, hosts),
    runs,
    settings,
    preferences,
    newRun: newRunMain(hosts, settings),
    surfaceUrl: surface,
    runWindows: windows.runs,
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
  // A machine that slept has dead connections nobody has noticed yet: check them now.
  powerMonitor.on('resume', () => manager.wake());
  watchNetwork(manager);
  return desktop;
}

/**
 * The app's windows: the main one, on the trusted `surface`, and the run windows, each the shell's
 * Run screen alone; losing a run window's surface closes only that window. `broadcast` sends one
 * ordered event to every window's surface.
 */
function appWindows(
  runs: RunControl,
  changed: () => void,
  surface: string,
  fatal: (error: Error) => void,
) {
  const shared = {
    log,
    connect: (port: RelayPort, run: RunRef) => runs.connect(port, run),
    logs: (run: RunRef, request: RunLogsRequest) => runs.logs(run, request),
    closed: (run: RunRef) => {
      runs.detach(run);
      changed();
    },
  };
  const main = new AppWindow({...shared, surfaceUrl: surface, fatal});
  const runWindows = new RunWindows({
    shellUrl: SHELL_URL,
    create: (url, title, closed) => {
      const runWindow: AppWindow = new AppWindow({
        ...shared,
        surfaceUrl: url,
        title,
        windowClosed: closed,
        fatal: error => {
          log(error.message);
          runWindow.close();
        },
      });
      return runWindow;
    },
  });
  return {
    main,
    runs: runWindows,
    broadcast<T>(channel: string, event: WelcomeEvent<T>): void {
      main.sendWelcome(channel, event);
      runWindows.sendWelcome(channel, event);
    },
  };
}

/** The machines, over the settings store and the hosts' checkouts and connections. */
function machinesOver(settings: SettingsStore, hosts: Hosts): MachinesMain {
  return new MachinesMain({
    settings,
    removeHost: id => hosts.remove(id),
    checkout: id => hosts.checkout(id),
    connection: id => hosts.connection(id),
  });
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
  let ownLaunch: ReturnType<typeof prepareLaunchHandoff>;
  try {
    // Resolve here, in the process that owns this command line. A relative cwd handed to the
    // existing primary would otherwise be resolved against that other process's working directory.
    const cwd = resolve(process.env['INIT_CWD'] ?? '.');
    ownLaunch = prepareLaunchHandoff(process.argv.slice(app.isPackaged ? 1 : 2), cwd);
  } catch (error) {
    log((error as Error).message);
    process.exit(2);
  }
  registerAppScheme();
  const launches = new AppLaunches<LaunchPlan>(error => log(error.message));
  let lifecycle: AppLifecycle;
  const startup = new AppStartup<LaunchPlan, ConnectionManager, Desktop>({
    ready: () => app.whenReady(),
    connectionManager,
    openWindow: manager =>
      openWindow(manager, error => {
        log(error.message);
        lifecycle.quit(1);
      }),
    openInitial: (desktop, initial) => launches.ready(desktop, initial),
    log,
  });
  lifecycle = new AppLifecycle({
    // Runs are detached and keep going; quitting ends this app's channels and ssh masters only.
    release: () => startup.release(),
    // The app's one exit call site.
    exit: code => app.exit(code),
    scheduleTimeout: realTimers,
    releaseBoundMs: RELEASE_BOUND_MS,
  });
  // One instance per user-data directory, the settings files' only writer: a second launch hands
  // its command line to this one (`second-instance`) and exits.
  if (!app.requestSingleInstanceLock(ownLaunch.handoff)) {
    log('VibeSys is already running; showing its window');
    lifecycle.quit(0);
    return;
  }
  app.on('second-instance', (_event, argv, cwd, additionalData) => {
    let second: LaunchPlan;
    try {
      second = planFromSecondInstance({
        electronArgv: argv,
        electronCwd: cwd,
        additionalData,
      });
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
  void startup.start(ownLaunch.plan, lifecycle);
}

main();
