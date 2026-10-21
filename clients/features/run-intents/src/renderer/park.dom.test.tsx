import {afterAll, beforeAll, expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge, GatewayCall} from '@vibesys/ipc';
import {HostCallRequestSchema, HostCallResponseSchema} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {RunIntents} from './index.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

/** The byte transport holds admissions until the simulated host acknowledges them. */
class FakeBridge implements GatewayBridge {
  readonly calls: Array<{bytes: Uint8Array; admit: (bytes: Uint8Array) => void}> = [];
  watching = false;
  unary(_host: HostKey, _method: string, bytes: Uint8Array): GatewayCall<Uint8Array> {
    const {promise, resolve, reject} = Promise.withResolvers<Uint8Array>();
    this.calls.push({bytes, admit: resolve});
    return {result: promise, cancel: () => reject(new Error('cancelled'))};
  }
  stream() {
    this.watching = true;
    return {
      cancel: () => {
        this.watching = false;
      },
    };
  }
}

test('Park is enabled on the new path and sends the typed host intent', async () => {
  const bridge = new FakeBridge();
  const clock = new FakeClock();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () =>
      root.render(
        <RunIntents
          bridge={bridge}
          host={'local' as HostKey}
          projectRoot="/project"
          runId="run-1"
          enabled
          schedule={clock.schedule}
        />,
      ),
    );
    const park = [...container.querySelectorAll('button')].find(
      button => button.textContent === 'Park',
    );
    expect(park).toBeDefined();
    expect(park?.disabled).toBe(false);
    await act(async () => park?.click());
    expect(bridge.calls).toHaveLength(1);
    const call = bridge.calls[0];
    if (!call) throw new Error('no host call');
    const envelope = fromBinary(HostCallRequestSchema, call.bytes);
    if (envelope.method.case !== 'postIntent') throw new Error('wrong host method');
    const requestId = envelope.method.value.requestId;
    expect(envelope.method.value.intent.case).toBe('park');
    expect(envelope.method.value.runId).toBe('run-1');
    await act(async () =>
      call.admit(
        toBinary(
          HostCallResponseSchema,
          create(HostCallResponseSchema, {
            result: {
              case: 'postIntent',
              value: {requestId, orderedReader: true},
            },
          }),
        ),
      ),
    );
    expect(container.textContent).toContain('Park · Pending');
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
  expect(bridge.watching).toBe(false);
});
