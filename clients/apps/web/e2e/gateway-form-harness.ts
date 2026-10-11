import {createGatewayConnectStore, GatewayForm} from '@vibesys/feature-gateway-connect/renderer';
import {createElement, useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';

interface Exchange {
  readonly reject: (reason: unknown) => void;
  readonly resolve: (browserSession: string) => void;
}

class FakeBootstrap {
  readonly exchanges: Exchange[] = [];

  readonly bootstrap = (_url: string, _signal: AbortSignal): Promise<string> => {
    let resolve = (_browserSession: string): void => undefined;
    let reject = (_reason: unknown): void => undefined;
    const promise = new Promise<string>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise;
      reject = rejectPromise;
    });
    this.exchanges.push({resolve, reject});
    return promise;
  };

  rejectCurrent(message: string): void {
    this.current().reject(new Error(message));
  }

  resolveCurrent(browserSession: string): void {
    this.current().resolve(browserSession);
  }

  private current(): Exchange {
    const exchange = this.exchanges.at(-1);
    if (exchange === undefined) throw new Error('No gateway exchange is pending');
    return exchange;
  }
}

declare global {
  interface Window {
    gatewayHarness: {
      readonly rejectCurrent: (message: string) => void;
      readonly resolveCurrent: (browserSession: string) => void;
    };
  }
}

const bootstrap = new FakeBootstrap();
const store = createGatewayConnectStore({
  page: window.location.href,
  initialError: null,
  onConnected: () => undefined,
  bootstrap: bootstrap.bootstrap,
});

function Harness() {
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  return createElement(GatewayForm, {state, send: store.dispatch});
}

const root = document.getElementById('root');
if (root === null) throw new Error('Gateway form harness root is missing');
createRoot(root).render(createElement(Harness));

window.gatewayHarness = {
  rejectCurrent: message => bootstrap.rejectCurrent(message),
  resolveCurrent: browserSession => bootstrap.resolveCurrent(browserSession),
};
