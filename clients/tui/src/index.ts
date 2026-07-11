import {CliRenderEvents, createCliRenderer} from '@opentui/core';
import {createOpenTuiApp} from './app.js';
import {SupervisionClient} from './client.js';

const socketPath = process.env['VIBESERVE_CONTROL_SOCKET'];
if (!socketPath) throw new Error('VIBESERVE_CONTROL_SOCKET is required');

const client = await SupervisionClient.connect(socketPath);
const renderer = await createCliRenderer({exitOnCtrlC: true});
const app = createOpenTuiApp(renderer, client);
renderer.start();

await new Promise<void>(resolve => renderer.once(CliRenderEvents.DESTROY, resolve));
app.destroy();
await client.close();
