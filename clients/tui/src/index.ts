import {CliRenderEvents, createCliRenderer} from '@opentui/core';
import {SupervisionClient} from './client.js';
import {SocketSessionController} from './session-controller.js';
import {createOpenTuiApp} from './ui/app.js';

const socketPath = process.env['VIBESYS_CONTROL_SOCKET'];
if (!socketPath) throw new Error('VIBESYS_CONTROL_SOCKET is required');

const client = await SupervisionClient.connect(socketPath);
const renderer = await createCliRenderer({exitOnCtrlC: true});
const controller = new SocketSessionController(client);
const app = createOpenTuiApp(renderer, controller);
renderer.start();
await controller.start();

await new Promise<void>(resolve => renderer.once(CliRenderEvents.DESTROY, resolve));
app.destroy();
await controller.stop();
