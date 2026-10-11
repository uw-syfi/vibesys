import {describe, expect, test} from 'bun:test';
import type {GatewayConnection} from '../../../entities/gateway.js';
import {createGatewayConnectStore} from './gateway-connect.js';

const PAGE = 'http://127.0.0.1:5173/';
const CAPABILITY = 'http://127.0.0.1:8765/?token=secret';

interface Exchange {
  readonly url: string;
  readonly signal: AbortSignal;
  readonly resolve: (browserSession: string) => void;
  readonly reject: (reason: unknown) => void;
}

class FakeBootstrap {
  readonly exchanges: Exchange[] = [];

  readonly bootstrap = (url: string, signal: AbortSignal): Promise<string> => {
    let resolve = (_browserSession: string): void => undefined;
    let reject = (_reason: unknown): void => undefined;
    const promise = new Promise<string>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise;
      reject = rejectPromise;
    });
    this.exchanges.push({url, signal, resolve, reject});
    return promise;
  };
}

describe('createGatewayConnectStore', () => {
  test('cancels the owned exchange, ignores its late success, and connects after retry', async () => {
    const fake = new FakeBootstrap();
    const connected: GatewayConnection[] = [];
    const store = createGatewayConnectStore({
      page: PAGE,
      initialError: null,
      onConnected: connection => connected.push(connection),
      bootstrap: fake.bootstrap,
    });

    store.dispatch({type: 'edit', value: CAPABILITY});
    store.dispatch({type: 'submit'});
    expect(fake.exchanges).toHaveLength(1);
    expect(fake.exchanges[0]?.url).toBe(CAPABILITY);

    store.dispatch({type: 'cancel'});
    expect(fake.exchanges[0]?.signal.aborted).toBe(true);
    expect(store.getState()).toMatchObject({
      kind: 'editing',
      retryTarget: {bootstrapUrl: CAPABILITY},
    });

    fake.exchanges[0]?.resolve('late-session');
    await Promise.resolve();
    await Promise.resolve();
    expect(connected).toEqual([]);

    store.dispatch({type: 'retry'});
    expect(fake.exchanges).toHaveLength(2);
    fake.exchanges[1]?.resolve('current-session');
    await Promise.resolve();
    await Promise.resolve();

    expect(connected).toHaveLength(1);
    expect(connected[0]).toMatchObject({
      webSocketUrl: 'ws://127.0.0.1:8765/ws',
      browserSession: 'current-session',
    });
    expect(JSON.stringify(connected[0])).not.toContain('secret');
  });
});
