// @ts-check
import './pin-performance-global.mjs';
import {firstLoad} from './first-load.mjs';
/** Real Electron composition for the gateway bridge's socket and stdio integration cases. */
import {spawn} from 'node:child_process';
import {connect} from 'node:net';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {fromChild, GatewayCalls, gatewayProcess} from '@vibesys/connection';
import {API_SCHEMA_HASH} from '@vibesys/proto';
import {SettingsStore} from '@vibesys/settings';
import {app, BrowserWindow, ipcMain, protocol} from 'electron';
// Resolve the built entry at runtime, as the other Electron E2E compositions do.
/** @type {typeof import('../src/main/gateway-ipc.js')} */
const {installGatewayIpc} = await import(new URL('../dist/main/gateway-ipc.js', import.meta.url).href);

/** @type {typeof globalThis & {
 * vibesysGatewayReady?: Promise<void>,
 * vibesysGatewayClosed?: Promise<void>
 * }} */
const gatewayState = globalThis;
const root = process.env['VIBESYS_GATEWAY_ROOT'];
const binary = process.env['VIBESYS_GATEWAY_BINARY'];
const mode = process.env['VIBESYS_GATEWAY_MODE'];
if (!root || !binary || (mode !== 'socket' && mode !== 'connect pipe'))
  throw new Error('gateway fixture configuration is missing');
const settings = new SettingsStore({
  settingsPath: join(root, 'settings', 'hosts.json'),
  recentPath: join(root, 'settings', 'recent.json'),
  preferencesPath: join(root, 'settings', 'preferences.json'),
  log: message => console.error(message),
});
/** @type {import('@vibesys/transport').ScheduleTimeout} */
const schedule = (callback, delay) => {
  const timer = setTimeout(callback, delay);
  return () => clearTimeout(timer);
};
const calls = new GatewayCalls(
  async host => host === 'local',
  () => ({
    dial: async () => {
      const duplex =
        mode === 'socket'
          ? connect(join(root, 'gateway', API_SCHEMA_HASH.slice(0, 16), 'sock'))
          : gatewayProcess(
              fromChild(spawn(binary, ['connect', '--root', root], {stdio: 'pipe'})),
              schedule,
            );
      gatewayState.vibesysGatewayClosed = new Promise(resolve =>
        duplex.once('close', () => resolve()),
      );
      return duplex;
    },
  }),
  schedule,
);

protocol.registerSchemesAsPrivileged([{scheme: 'app', privileges: {standard: true, secure: true}}]);
// Playwright releases app.whenReady after module evaluation; expose its promise without awaiting here.
gatewayState.vibesysGatewayReady = app.whenReady().then(async () => {
  protocol.handle(
    'app',
    () =>
      new Response('<!doctype html><html><body></body></html>', {
        headers: {'content-type': 'text/html'},
      }),
  );
  ipcMain.on('vibesys:welcome:first-paint', event => {
    event.returnValue = {theme: 'ink', chromeInset: {width: 0, height: 0}};
  });
  const page = 'app://vibesys/gateway.html';
  const window = new BrowserWindow({
    webPreferences: {
      preload: fileURLToPath(new URL('../dist/preload/welcome.cjs', import.meta.url)),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });
  installGatewayIpc(ipcMain, {
    calls,
    settings,
    admits: event => event.senderId === window.webContents.id && event.senderUrl === page,
  });
  app.on('before-quit', () => calls.close());
  app.on('window-all-closed', () => app.quit());
  const loaded = firstLoad(window.webContents);
  void window.loadURL(page).catch(error => console.error(error));
  await loaded;
});
gatewayState.vibesysGatewayReady.catch(error => {
  console.error(error);
  app.exit(1);
});
