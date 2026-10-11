/**
 * Electron main process: the composition root. It reads what the app was asked to open, takes the
 * single-instance lock, and wires the lifecycle (`app-lifecycle.ts`), the page confinement
 * (`app-security.ts`), the settings store, the hosts, and the window (`desktop-app.ts`).
 */
import {homedir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {app, Menu} from 'electron';
import {AppLifecycle, realTimers} from './app-lifecycle.js';
import {
  confineContents,
  lockDownSession,
  registerAppScheme,
  serveBundledUi,
} from './app-security.js';
import {controlDirectory, installAskpass} from './askpass.js';
import {DesktopApp} from './desktop-app.js';
import {HostPool, systemHostFactory} from './host-pool.js';
import {type LaunchPlan, parseLaunch} from './launch-args.js';
import {SettingsStore} from './settings-store.js';

/** The repository checkout this app was built in: This Mac's VibeSys checkout by default. */
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url)).replace(/\/+$/, '');
/** How long quitting waits for the hosts to be released before exiting anyway. */
const RELEASE_BOUND_MS = 5_000;

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

async function openWindow(): Promise<{desktop: DesktopApp; pool: HostPool}> {
  lockDownSession(log);
  serveBundledUi();
  Menu.setApplicationMenu(applicationMenu());
  const userData = app.getPath('userData');
  const settings = new SettingsStore({
    settingsPath: join(userData, 'hosts.json'),
    recentPath: join(userData, 'recent.json'),
    log,
  });
  const pool = new HostPool({
    sshConfigPath: join(homedir(), '.ssh', 'config'),
    settings,
    localCheckout: REPOSITORY_ROOT,
    factory: systemHostFactory({
      controlPath: join(await controlDirectory(process.getuid?.() ?? 0), '%C'),
      askpass: await installAskpass(join(userData, 'askpass.sh')),
    }),
    log,
  });
  const desktop = new DesktopApp({pool, scheduleTimeout: realTimers, log});
  settings.onRecentChanged(recent => desktop.recentChanged(recent));
  desktop.bind();
  return {desktop, pool};
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
  let opened: {desktop: DesktopApp; pool: HostPool} | null = null;
  const lifecycle = new AppLifecycle({
    // Runs are detached and keep going; quitting ends this app's streams and SSH masters only.
    release: async () => opened?.pool.closeAll(),
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
    if (opened === null) return;
    opened.desktop.focus();
    let second: LaunchPlan;
    try {
      second = planFrom(argv, cwd);
    } catch (error) {
      log(`ignoring a second launch's arguments: ${(error as Error).message}`);
      return;
    }
    opened.desktop.open(second).catch((error: Error) => log(error.message));
  });
  app.on('will-quit', event => lifecycle.willQuit(event));
  for (const signal of ['SIGINT', 'SIGTERM'] as const) process.once(signal, () => app.quit());
  app.on('web-contents-created', (_event, contents) => confineContents(contents, log));
  app.on('window-all-closed', () => app.quit());
  void app.whenReady().then(async () => {
    opened = await openWindow();
    try {
      await opened.desktop.open(plan);
    } catch (error) {
      log((error as Error).message);
      lifecycle.quit(1);
    }
  });
}

main();
