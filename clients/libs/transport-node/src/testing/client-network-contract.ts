import {strict as assert} from 'node:assert';
import {describe, it} from 'node:test';
import type {ClientNetwork} from '../client-network.js';

/** One peer controlled by the shared `ClientNetwork` contract suite. */
export interface ClientNetworkContractPeer {
  readonly network: ClientNetwork;
  readonly path: string;
  establish(): Promise<void>;
  clientWrites(): Promise<readonly string[]>;
  sendToClient(text: string): void;
  endPeer(): void;
  dispose(): Promise<void>;
}

/**
 * The behavior every `ClientNetwork` implementation owes `ServerClient`.
 * Real adapters gate the whole suite; the in-memory Fake runs on every pass.
 */
export function clientNetworkContract(
  name: string,
  makePeer: () => Promise<ClientNetworkContractPeer>,
  skip = false,
): void {
  describe(`ClientNetwork contract (${name})`, {skip}, () => {
    it('connects, exchanges text, detaches listeners, and reports terminal close', async () => {
      const peer = await makePeer();
      try {
        const socket = peer.network.connect(peer.path);
        const connected = notification();
        const data = notification<string>();
        const peerEnded = notification();
        const closed = notification();
        let detachedData = false;
        let socketError: Error | null = null;
        socket.listenConnect(connected.notify);
        const removeDetached = socket.listenData(() => {
          detachedData = true;
        });
        removeDetached();
        removeDetached();
        socket.listenData(data.notify);
        socket.listenError(error => {
          socketError = error;
        });
        socket.listenPeerEnd(peerEnded.notify);
        socket.listenClose(closed.notify);

        await peer.establish();
        await connected.value;
        socket.setTextMode();
        socket.send('from-client\n');
        assert.deepEqual(await peer.clientWrites(), ['from-client\n']);

        peer.sendToClient('from-server\n');
        assert.equal(await data.value, 'from-server\n');
        assert.equal(detachedData, false);

        peer.endPeer();
        await Promise.all([peerEnded.value, closed.value]);
        assert.equal(socket.closed, true);
        assert.equal(socketError, null);
      } finally {
        await peer.dispose();
      }
    });
  });
}

interface Notification<T = void> {
  readonly value: Promise<T>;
  notify(value: T): void;
}

/** A one-shot event controlled by the contract peer, never by elapsed time. */
function notification<T = void>(): Notification<T> {
  let notify = (_value: T): void => {};
  const value = new Promise<T>(resolve => {
    notify = resolve;
  });
  return {value, notify};
}
