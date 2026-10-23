import {afterAll, beforeAll, expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {type HostKey, recordedProjectRoot} from '@vibesys/hosts';
import type {GatewayBridge, GatewayCall} from '@vibesys/ipc';
import {
  LifecycleSchema,
  RecordSchema,
  RunLiveness,
  type Record as StoreRecord,
} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {FakeRecords} from './fake-records.js';
import {RunIntents} from './index.js';

const encodeRecord = (record: StoreRecord) => toBinary(RecordSchema, record);

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
  stream(): never {
    throw new Error('the controls hold no host stream: the page owns the one liveness lease');
  }
}

test('controls take the page writer facts and all owned work cancels when disabled', async () => {
  const bridge = new FakeTransport();
  const feed = new FakeRecords(encodeRecord);
  const clock = new FakeClock();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const render = (enabled: boolean, observed: boolean) =>
    act(async () =>
      root.render(
        <RunIntents
          bridge={bridge}
          records={feed.subscribe}
          host={'local' as HostKey}
          projectRoot={recordedProjectRoot('/project')}
          runId="run-1"
          enabled={enabled}
          schedule={clock.schedule}
          writer={{
            observation: observed
              ? {
                  epoch: 1n,
                  lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
                }
              : undefined,
            liveness: {epoch: 1n, value: RunLiveness.ENDED},
            fresh: true,
          }}
        />,
      ),
    );
  try {
    await render(false, false);
    await render(true, false);
    expect(feed.subscribers).toBe(1);
    await render(true, true);
    const relaunch = [...container.querySelectorAll('button')].find(
      button => button.textContent === 'Resume',
    );
    expect(relaunch).toBeDefined();
    expect(feed.subscribers).toBe(1);
    await act(async () => relaunch?.click());
    expect(bridge.launches).toBe(1);
    await render(false, true);
    expect(container.textContent).toBe('');
    expect(feed.subscribers).toBe(0);
    expect(bridge.cancelled).toBe(true);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
