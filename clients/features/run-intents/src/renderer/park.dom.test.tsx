import {afterAll, beforeAll, expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {type HostKey, recordedProjectRoot} from '@vibesys/hosts';
import type {GatewayBridge, GatewayCall} from '@vibesys/ipc';
import {
  HostCallRequestSchema,
  HostCallResponseSchema,
  IntentAction,
  IntentStep,
  type Lifecycle,
  LifecycleSchema,
  RecordSchema,
  RunLiveness,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {FakeRecords} from './fake-records.js';
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
  readonly liveness = new Set<(bytes: Uint8Array) => void>();
  unary(_host: HostKey, _method: string, bytes: Uint8Array): GatewayCall<Uint8Array> {
    const {promise, resolve, reject} = Promise.withResolvers<Uint8Array>();
    this.calls.push({bytes, admit: resolve});
    return {result: promise, cancel: () => reject(new Error('cancelled'))};
  }
  stream(
    _host: HostKey,
    method: string,
    _request: Uint8Array,
    receive: (bytes: Uint8Array) => void,
  ) {
    if (method.endsWith('/WatchRuns')) {
      this.liveness.add(receive);
      this.alive(1n, true);
    }
    return {
      cancel: () => {
        this.liveness.delete(receive);
      },
    };
  }
  alive(epoch: bigint, live: boolean) {
    const bytes = toBinary(
      WatchRunsResponseSchema,
      create(WatchRunsResponseSchema, {
        runId: 'run-1',
        head: {epoch: Number(epoch)},
        liveness: live ? RunLiveness.ALIVE : RunLiveness.ENDED,
      }),
    );
    for (const receive of this.liveness) receive(bytes);
  }
}

test('Pause is enabled on the new path and sends the typed host intent', async () => {
  const bridge = new FakeBridge();
  const feed = new FakeRecords();
  const clock = new FakeClock();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () =>
      root.render(
        <RunIntents
          bridge={bridge}
          records={feed.subscribe}
          host={'local' as HostKey}
          projectRoot={recordedProjectRoot('/project')}
          runId="run-1"
          enabled
          relaunch={{
            epoch: 1n,
            lifecycle: create(LifecycleSchema, {state: {case: 'running', value: {}}}),
          }}
          schedule={clock.schedule}
        />,
      ),
    );
    const park = [...container.querySelectorAll('button')].find(
      button => button.textContent === 'Pause',
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
    expect(envelope.method.value.intent.case).toBe('pause');
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
    expect(container.textContent).toContain('Pause · Pending');
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
  expect(feed.subscribers).toBe(0);
});

test('live, draining, parked, resumed and finished observations keep the right controls and history', async () => {
  const bridge = new FakeBridge();
  const feed = new FakeRecords();
  const clock = new FakeClock();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const render = async (epoch: bigint, lifecycle: Lifecycle) => {
    await act(async () =>
      root.render(
        <RunIntents
          bridge={bridge}
          records={feed.subscribe}
          host={'local' as HostKey}
          projectRoot={recordedProjectRoot('/project')}
          runId="run-1"
          enabled
          relaunch={{epoch, lifecycle}}
          schedule={clock.schedule}
        />,
      ),
    );
    await act(async () =>
      bridge.alive(epoch, !['parked', 'ended'].includes(lifecycle.state.case ?? '')),
    );
  };
  const labels = () => [...container.querySelectorAll('button')].map(button => button.textContent);
  try {
    await render(1n, create(LifecycleSchema, {state: {case: 'running', value: {}}}));
    expect(labels()).toContain('Pause now');
    await act(async () =>
      feed.publish([
        create(RecordSchema, {
          seq: 1n,
          epoch: 1,
          body: {
            case: 'intent',
            value: {
              controlId: 'remote-pause',
              action: IntentAction.PAUSE,
              step: IntentStep.APPLIED,
            },
          },
        }),
      ]),
    );
    expect(container.textContent).toContain('Pause · Applied');
    await render(1n, create(LifecycleSchema, {state: {case: 'draining', value: {}}}));
    expect(labels()).toContain('Pause now');
    expect(labels()).not.toContain('Pause');
    await render(1n, create(LifecycleSchema, {state: {case: 'parked', value: {}}}));
    expect(labels()).toContain('Resume');
    expect(labels()).not.toContain('Pause now');
    const resume = [...container.querySelectorAll('button')].find(
      button => button.textContent === 'Resume',
    );
    await act(async () => resume?.click());
    const call = bridge.calls.at(-1);
    if (!call) throw new Error('missing relaunch');
    await act(async () =>
      call.admit(
        toBinary(
          HostCallResponseSchema,
          create(HostCallResponseSchema, {
            result: {case: 'relaunch', value: {outcome: {case: 'epoch', value: 2n}}},
          }),
        ),
      ),
    );
    await render(2n, create(LifecycleSchema, {state: {case: 'running', value: {}}}));
    expect(container.textContent).toContain('Resume · Applied');
    expect(container.textContent).toContain('Request history');
    expect(labels()).toContain('Pause now');
    await render(2n, create(LifecycleSchema, {state: {case: 'ended', value: {resumable: false}}}));
    expect(labels()).not.toContain('Pause');
    expect(labels()).not.toContain('Pause now');
    // The host admits Resume after any ending; the flag it keeps for older readers does not narrow it.
    expect(labels().filter(label => label === 'Resume')).toEqual(['Resume']);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
