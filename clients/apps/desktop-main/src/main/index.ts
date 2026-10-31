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
  LoginActor,
  MUTATE_METHOD,
  READ_METHOD,
  registryClusters,
  registryProjects,
  registryReads,
  registryRunCatalog,
  registryRunReads,
  registrySavedRuns,
  registrySetup,
  registryStart,
  runWatch,
  SshLink,
  Subscriptions,
  WATCH_METHOD,
  WATCH_RUNS_METHOD,
} from '@vibesys/connection';
import {RunNotifier} from '@vibesys/feature-app-shell/main';
import {Hosts} from '@vibesys/feature-hosts/main';
import {MachinesMain} from '@vibesys/feature-machines/main';
import {NewRunMain} from '@vibesys/feature-new-run/main';
import {type RelayPort, RunControl} from '@vibesys/feature-run-control/main';
import {PreferencesMain} from '@vibesys/feature-settings/main';
import {type HostId, type HostListing, hostFromKey, hostKey} from '@vibesys/hosts';
import {
  FIRST_PAINT_CHANNEL,
  type FirstPaint,
  WELCOME_EVENTS,
  type WelcomeEvent,
} from '@vibesys/ipc';
import {RunHistoryStore} from '@vibesys/run-history';
import type {RunRef} from '@vibesys/runs';
import {remember, SettingsStore} from '@vibesys/settings';
import {app, ipcMain, Menu, Notification, nativeTheme, net, powerMonitor, shell} from 'electron';
import {SHELL_URL} from './app-assets.js';
import {AppLaunches} from './app-launches.js';
import {AppLifecycle, realTimers} from './app-lifecycle.js';
import {applicationMenuTemplate} from './app-menu.js';
import {
  confineContents,
  lockDownSession,
  registerAppScheme,
  serveBundledUi,
} from './app-security.js';
import {AppStartup} from './app-startup.js';
import {effectiveCheckout} from './checkout.js';
import {Desktop} from './desktop.js';
import {createGatewayCalls, installGatewayIpc} from './gateway-ipc.js';
import {type LaunchPlan, userDataOverride} from './launch-args.js';
import {planFromSecondInstance, prepareLaunchHandoff} from './launch-handoff.js';
import {RunWindows} from './run-windows.js';
import {installSubscriptionIpc} from './subscription-ipc.js';
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

/** Report the network coming back (offline to online) to everything that holds a connection. */
function watchNetwork(wake: () => void): void {
  let online = true;
  const poll = (): void => {
    const now = net.isOnline();
    if (now && !online) wake();
    online = now;
    realTimers(poll, NETWORK_POLL_MS);
  };
  realTimers(poll, NETWORK_POLL_MS);
}

/** New run's requests, answered over the hosts' connections, recording each start in Recent. */
function newRunMain(
  hosts: Hosts,
  settings: SettingsStore,
  gateway: ReturnType<typeof installGateway>,
  runs: ReturnType<typeof runWatch>,
): NewRunMain {
  const starts = registryStart({
    checkout: id => hosts.checkout(id),
    unary: (id, bytes, signal) => gateway.unary(hostKey(id), MUTATE_METHOD, bytes, signal),
  });
  return new NewRunMain({
    remember: run => settings.updateRecent(recent => remember(recent, run)),
    now: () => Date.now(),
    registryStart: (host, inputs, signal) => starts.start(host, inputs, signal),
    registryReads: registryReads({
      checkout: id => hosts.checkout(id),
      unary: (id, bytes, signal) => gateway.unary(hostKey(id), READ_METHOD, bytes, signal),
    }),
    registryCatalog: registryRunCatalog({
      checkout: id => hosts.checkout(id),
      unary: (id, bytes, signal) => gateway.unary(hostKey(id), READ_METHOD, bytes, signal),
    }),
    registryProjects: registryProjects({
      checkout: id => hosts.checkout(id),
      read: (id, bytes, signal) => gateway.unary(hostKey(id), READ_METHOD, bytes, signal),
      mutate: (id, bytes, signal) => gateway.unary(hostKey(id), MUTATE_METHOD, bytes, signal),
    }),
    watchRuns: runs,
  });
}

/** Install the application menu, whose items act on the main window as it is when chosen. */
function installApplicationMenu(main: () => AppWindow): void {
  Menu.setApplicationMenu(
    Menu.buildFromTemplate(
      applicationMenuTemplate(process.platform, app.name, {
        openSettings: () => {
          main().openSettings();
          main().focus();
        },
      }),
    ),
  );
}

function installFirstPaint(preferences: PreferencesMain): void {
  ipcMain.on(FIRST_PAINT_CHANNEL, event => {
    const firstPaint: FirstPaint = {
      theme: preferences.theme(),
      chromeInset: chromeInset(process.platform),
    };
    event.returnValue = firstPaint;
  });
}

function runNotifier(preferences: PreferencesMain, focus: () => void): RunNotifier {
  return new RunNotifier({
    enabled: () => preferences.notificationsEnabled(),
    notify: notice => {
      const notification = new Notification({title: notice.title, body: notice.body});
      notification.on('click', () => focus());
      notification.show();
    },
  });
}

function watchHostRuns(gateway: ReturnType<typeof installGateway>, manager: ConnectionManager) {
  const runs = runWatch(
    {
      stream: (host, bytes, signal, receive, reached) =>
        gateway.stream(hostKey(host), WATCH_RUNS_METHOD, bytes, signal, receive, reached),
    },
    realTimers,
  );
  manager.onDisposing(() => runs.close());
  return runs;
}

function installGateway(manager: ConnectionManager) {
  return createGatewayCalls(
    manager,
    fileURLToPath(new URL('../gateway/', import.meta.url)),
    realTimers,
    log,
  );
}

/** The hub of live subscriptions, over the gateway, closed with the connections it rides. */
function startSubscriptions(
  gateway: ReturnType<typeof installGateway>,
  manager: ConnectionManager,
): Subscriptions {
  const subscriptions = new Subscriptions(
    gateway,
    realTimers,
    () => Date.now(),
    error => log(`a subscription observer failed: ${String(error)}`),
  );
  manager.onDisposing(() => subscriptions.close());
  return subscriptions;
}

/** The two ports a window reaches hosts through: the generic gateway, and subscriptions. */
function installPorts(parts: {
  readonly gateway: ReturnType<typeof installGateway>;
  readonly subscriptions: Subscriptions;
  readonly settings: SettingsStore;
  readonly admits: Parameters<typeof installGatewayIpc>[1]['admits'];
}): void {
  installGatewayIpc(ipcMain, {
    calls: parts.gateway,
    checkout: effectiveCheckout(parts.settings, REPOSITORY_ROOT),
    admits: parts.admits,
  });
  installSubscriptionIpc(ipcMain, {hub: parts.subscriptions.port, admits: parts.admits});
}

async function openWindow(
  manager: ConnectionManager,
  fatal: (error: Error) => void,
): Promise<Desktop> {
  lockDownSession(log);
  serveBundledUi();
  installApplicationMenu(() => windows.main);
  const userData = app.getPath('userData');
  const settings = new SettingsStore({
    settingsPath: join(userData, 'hosts.json'),
    recentPath: join(userData, 'recent.json'),
    preferencesPath: join(userData, 'preferences.json'),
    log,
  });
  const gateway = installGateway(manager);
  const subscriptions = startSubscriptions(gateway, manager);
  const watchedRuns = watchHostRuns(gateway, manager);
  const welcome = new WelcomeFeed();
  const preferences = new PreferencesMain({
    store: settings,
    nativeTheme,
    push: value => windows.broadcast(WELCOME_EVENTS.preferences, welcome.preferences(value)),
  });
  await preferences.load();
  // The preload asks for this before the page's scripts run, so the first paint is in the theme
  // and clear of the window controls.
  installFirstPaint(preferences);
  const notifier = runNotifier(preferences, () => windows.main.focus());
  // The one copy of every ended run's recorded events, shared by all windows. It reads the host
  // through `hosts` (below), and forgets a run the host lists as live again or a host that goes.
  const history = new RunHistoryStore({
    source: {
      read: (run, from, max, kinds) =>
        hosts.readEvents(hostFromKey(run.host), run.project, run.runId, from, max, kinds),
    },
  });
  const held: {logins?: LoginActor} = {};
  const {hosts, setup} = listedHosts({
    manager,
    settings,
    gateway,
    history,
    notifier,
    welcome,
    broadcast: listing => windows.broadcast(WELCOME_EVENTS.listing, welcome.listing(listing)),
    endLogin: id => held.logins?.cancelHost(id),
    runs: watchedRuns,
    subscriptions,
  });
  const logins = startLogins(hosts, gateway, manager);
  held.logins = logins;
  const runs: RunControl = new RunControl({
    connection: id => hosts.connection(id),
    // A run shown in a window of its own is live there; any other run in the main window.
    openPage: run => (windows.runs.showing(run) ?? windows.main).bindRun(run),
    // Nothing shows the window's run state any more.
    changed: () => {},
    remember: (run, current) => settings.updateRecent(recent => remember(recent, run), current),
    log,
    now: () => Date.now(),
  });
  const windows = appWindows(runs, fatal);
  const desktop = new Desktop({
    window: windows.main,
    welcome,
    hosts,
    history,
    machines: machinesOver(settings, hosts, gateway, setup, logins, watchedRuns),
    runs,
    settings,
    preferences,
    newRun: newRunMain(hosts, settings, gateway, watchedRuns),
    runWindows: windows.runs,
    shellUrl: SHELL_URL,
    openExternal: url => shell.openExternal(url),
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
  const wake = (): void => {
    manager.wake();
    subscriptions.wake();
  };
  powerMonitor.on('resume', wake);
  watchNetwork(wake);
  const admits = desktop.admitsRenderer.bind(desktop);
  installPorts({gateway, subscriptions, settings, admits});
  return desktop;
}

/**
 * The app's windows: the main one, on the app shell, and the run windows, each the shell's Run
 * screen alone; losing a run window's surface closes only that window. `broadcast` sends one
 * ordered event to every window's surface.
 */
function appWindows(runs: RunControl, fatal: (error: Error) => void) {
  const shared = {
    log,
    connect: (port: RelayPort, run: RunRef) => runs.connect(port, run),
    closed: (run: RunRef) => runs.detach(run),
  };
  const main = new AppWindow({...shared, surfaceUrl: SHELL_URL, fatal});
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

function listedHosts(parts: {
  readonly manager: ConnectionManager;
  readonly settings: SettingsStore;
  readonly gateway: ReturnType<typeof installGateway>;
  readonly history: RunHistoryStore;
  readonly notifier: RunNotifier;
  readonly welcome: WelcomeFeed;
  readonly broadcast: (listing: HostListing) => void;
  readonly endLogin?: (id: HostId) => void;
  readonly runs: ReturnType<typeof runWatch>;
  /** Removing a host ends the subscriptions on it (`Hosts.remove`). */
  readonly subscriptions: Pick<Subscriptions, 'closeHost'>;
}): {readonly hosts: Hosts; readonly setup: ReturnType<typeof registrySetup>} {
  const {manager, settings, gateway, history, notifier, welcome, broadcast, endLogin, runs} = parts;
  const {subscriptions} = parts;
  const setup = registrySetup({
    unary: (id, bytes, signal) => gateway.unary(hostKey(id), READ_METHOD, bytes, signal),
  });
  return {
    setup,
    hosts: new Hosts({
      manager,
      settings,
      localCheckout: REPOSITORY_ROOT,
      sshConfigPath: join(homedir(), '.ssh', 'config'),
      setup,
      ...(endLogin === undefined ? {} : {endLogin}),
      runs,
      subscriptions,
      catalog: registryRunCatalog({
        checkout: effectiveCheckout(settings, REPOSITORY_ROOT),
        unary: (id, bytes, signal) => gateway.unary(hostKey(id), READ_METHOD, bytes, signal),
      }),
      savedRuns: registrySavedRuns({
        checkout: effectiveCheckout(settings, REPOSITORY_ROOT),
        stream: (id, bytes, signal, receive) =>
          gateway.stream(hostKey(id), WATCH_METHOD, bytes, signal, receive),
      }),
      runReads: registryRunReads({
        checkout: effectiveCheckout(settings, REPOSITORY_ROOT),
        unary: (id, bytes, signal) => gateway.unary(hostKey(id), READ_METHOD, bytes, signal),
      }),
      log,
      push: listing => {
        notifier.listing(listing);
        if (listing.kind === 'listed') {
          for (const run of listing.live) history.runsLive(listing.host, [run.runId]);
        }
        broadcast(listing);
      },
      retire: host => {
        history.invalidateHost(host);
        welcome.retireHost(host);
      },
    }),
  };
}

/** The login actor. It outlives a panel and closes with the connection manager. */
function startLogins(
  hosts: Hosts,
  gateway: ReturnType<typeof installGateway>,
  manager: ConnectionManager,
): LoginActor {
  const logins = new LoginActor({
    checkout: id => hosts.checkout(id),
    stream: (id, bytes, signal, receive) =>
      gateway.stream(hostKey(id), WATCH_METHOD, bytes, signal, receive),
  });
  manager.onDisposing(() => logins.close());
  return logins;
}

/** The machines, over the settings store and the hosts' checkouts and connections. */
function machinesOver(
  settings: SettingsStore,
  hosts: Hosts,
  gateway: ReturnType<typeof installGateway>,
  setup: ReturnType<typeof registrySetup>,
  logins: LoginActor,
  runs: ReturnType<typeof runWatch>,
): MachinesMain {
  return new MachinesMain({
    settings,
    removeHost: id => hosts.remove(id),
    checkout: id => hosts.checkout(id),
    registry: registryReads({
      checkout: id => hosts.checkout(id),
      unary: (id, bytes, signal) => gateway.unary(hostKey(id), READ_METHOD, bytes, signal),
    }),
    catalog: registryRunCatalog({
      checkout: id => hosts.checkout(id),
      unary: (id, bytes, signal) => gateway.unary(hostKey(id), READ_METHOD, bytes, signal),
    }),
    clusters: registryClusters({
      checkout: id => hosts.checkout(id),
      unary: (id, bytes, signal) => gateway.unary(hostKey(id), MUTATE_METHOD, bytes, signal),
    }),
    setup,
    logins,
    runs,
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
    // Before anything reads `userData`, including the single-instance lock keyed on it.
    const userData = userDataOverride(process.env);
    if (userData !== null) app.setPath('userData', userData);
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
