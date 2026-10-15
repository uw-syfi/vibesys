import {randomUUID} from 'node:crypto';
import {unlink} from 'node:fs/promises';
// test-isolation: the production network's opt-in contract drives its real Unix adapter.
import {createServer, type Server, type Socket} from 'node:net';
import {UnixClientNetwork} from './index.js';
import {
  type ClientNetworkContractPeer,
  clientNetworkContract,
  FakeClientNetwork,
} from './testing/index.js';

const realContracts = process.env['VIBESYS_REAL_CONTRACTS'] === '1';

clientNetworkContract('FakeClientNetwork', async () => fakePeer());
clientNetworkContract('UnixClientNetwork', realPeer, !realContracts);

function fakePeer(): ClientNetworkContractPeer {
  const network = new FakeClientNetwork();
  const socket = () => {
    const connection = network.connections[0];
    if (connection === undefined) throw new Error('the contract did not open a fake connection');
    return connection;
  };
  return {
    network,
    path: '/run/server.sock',
    establish: async () => socket().connect(),
    clientWrites: async () => socket().writes,
    sendToClient: text => socket().receive(text),
    endPeer: () => socket().peerEnd(),
    dispose: async () => socket().destroy(),
  };
}

async function realPeer(): Promise<ClientNetworkContractPeer> {
  const path = `/tmp/vs-network-${randomUUID().slice(0, 8)}.sock`;
  let accepted: Socket | null = null;
  let accept = (): void => {};
  const acceptedConnection = new Promise<void>(resolve => {
    accept = resolve;
  });
  let receive = (_text: readonly string[]): void => {};
  const received = new Promise<readonly string[]>(resolve => {
    receive = resolve;
  });
  const server = createServer(socket => {
    accepted = socket;
    socket.setEncoding('utf8');
    socket.once('data', chunk => receive([chunk.toString()]));
    accept();
  });
  await listen(server, path);
  const connectedPeer = (): Socket => {
    if (accepted === null) throw new Error('the Unix network contract has no accepted peer');
    return accepted;
  };
  return {
    network: new UnixClientNetwork(),
    path,
    establish: async () => acceptedConnection,
    clientWrites: async () => received,
    sendToClient: text => connectedPeer().write(text),
    endPeer: () => connectedPeer().end(),
    dispose: async () => {
      accepted?.destroy();
      await close(server);
      await unlink(path).catch(() => undefined);
    },
  };
}

function listen(server: Server, path: string): Promise<void> {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(path, resolve);
  });
}

function close(server: Server): Promise<void> {
  return new Promise((resolve, reject) => {
    server.close(error => {
      if (error) reject(error);
      else resolve();
    });
  });
}
