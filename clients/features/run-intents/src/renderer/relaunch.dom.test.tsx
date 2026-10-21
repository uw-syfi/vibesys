import {afterAll, beforeAll, expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge, GatewayCall} from '@vibesys/ipc';
import {LifecycleSchema} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {RunIntents} from './index.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

/** Hold the launch reply while observing exactly which calls the component owns. */
class FakeTransport implements GatewayBridge {
  streams = 0;
  launches = 0;
  cancelled = false;
  unary(_host: HostKey, _method: string, _bytes: Uint8Array): GatewayCall<Uint8Array> {
    const {promise, reject} = Promise.withResolvers<Uint8Array>();
    this.launches += 1;
    return {
      result: promise,
      cancel: () => {
        this.cancelled = true;
        reject(new Error('cancelled'));
      },
    };
  }
  stream() {
    this.streams += 1;
    let active = true;
    return {
      cancel: () => {
        if (!active) return;
        active = false;
        this.streams -= 1;
      },
    };
  }
}

test('Relaunch starts when observation becomes available and cancels when disabled', async () => {
  const bridge = new FakeTransport();
  const clock = new FakeClock();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const render = (enabled: boolean, observed: boolean) =>
    act(async () =>
      root.render(
        <RunIntents
          bridge={bridge}
          host={'local' as HostKey}
          projectRoot="/project"
          runId="run-1"
          enabled={enabled}
          schedule={clock.schedule}
          {...(observed
            ? {
                relaunch: {
                  epoch: 1n,
                  lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
                },
              }
            : {})}
        />,
      ),
    );
  try {
    await render(false, false);
    expect(bridge.streams).toBe(0);
    await render(true, false);
    expect(bridge.streams).toBe(1);
    await render(true, true);
    const relaunch = [...container.querySelectorAll('button')].find(
      button => button.textContent === 'Relaunch',
    );
    expect(relaunch).toBeDefined();
    expect(bridge.streams).toBe(2);
    await act(async () => relaunch?.click());
    expect(bridge.launches).toBe(1);
    await render(false, true);
    expect(container.textContent).toBe('');
    expect(bridge.streams).toBe(0);
    expect(bridge.cancelled).toBe(true);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
