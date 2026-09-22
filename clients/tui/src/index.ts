import {writeFile} from 'node:fs/promises';
import {createCliRenderer} from '@opentui/core';
import {ServerClient} from '@vibesys/backend-client/node';
import {resolveStartupTrace} from './boot-trace.js';
import {runTuiSession} from './runtime.js';
import {SocketSessionController} from './session-controller.js';
import {resolveStartupTheme} from './startup-theme.js';
import {createOpenTuiApp} from './ui/app.js';

/** The launcher starts the backend and this process concurrently. */
const CONNECT_TIMEOUT_MS = 30_000;

const socketPath = process.env['VIBESYS_CONTROL_SOCKET'];
if (!socketPath) throw new Error('VIBESYS_CONTROL_SOCKET is required');

const client = await ServerClient.connect(socketPath, {
  connectTimeoutMs: CONNECT_TIMEOUT_MS,
});
const explicitTheme = process.env['VIBESYS_THEME'];
// In flight while the renderer starts, so the configured theme costs no
// extra wall clock before the first frame.
const themeRequest =
  explicitTheme === undefined ? client.request({type: 'query.tui_defaults'}) : undefined;
// VibeSys owns Ctrl+C so a nonempty OpenTUI selection can be copied before the
// same chord falls back to exiting. Enabling OpenTUI's parallel exit handler
// would make those two outcomes race.
const renderer = await createCliRenderer({exitOnCtrlC: false});
const controller = new SocketSessionController(
  client,
  await resolveStartupTheme(themeRequest, {explicitTheme}),
  // Quiet unless VIBESYS_BOOT_TRACE=1; see boot-trace.ts.
  resolveStartupTrace(process.env, line => {
    process.stderr.write(`${line}\n`);
  }),
);
const app = createOpenTuiApp(renderer, controller);
const startupSmokeMarker = process.env['VIBESYS_RELEASE_SMOKE_MARKER'];
const completeStartupSmoke = startupSmokeMarker
  ? async () => {
      await writeFile(startupSmokeMarker, 'renderer initialized; control protocol exchanged\n', {
        flag: 'wx',
      });
    }
  : undefined;
await runTuiSession(renderer, controller, app, completeStartupSmoke);
