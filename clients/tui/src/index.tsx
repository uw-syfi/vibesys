import {render} from 'ink';
import {App} from './app.js';
import {SupervisionClient} from './client.js';

const socketPath = process.env['VIBESERVE_CONTROL_SOCKET'];
if (!socketPath) throw new Error('VIBESERVE_CONTROL_SOCKET is required');

const client = await SupervisionClient.connect(socketPath);
const instance = render(<App client={client}/>);
await instance.waitUntilExit();
await client.close();
