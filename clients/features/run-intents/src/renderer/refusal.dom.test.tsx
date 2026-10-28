/** A refused request, as the run controls show it: the host's reason on the page, not on hover. */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {type HostKey, recordedProjectRoot} from '@vibesys/hosts';
import {FakeGatewayBridge} from '@vibesys/ipc/testing';
import {
  HostCallResponseSchema,
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

const REASON = 'Refused by host';

function textNodes(container: Element): string[] {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const texts: string[] = [];
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode())
    texts.push(node.textContent ?? '');
  return texts;
}

test('a refused request says why on the page, not in a tooltip', async () => {
  const bridge = new FakeGatewayBridge();
  const feed = new FakeRecords((record: StoreRecord) => toBinary(RecordSchema, record));
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
          writer={{
            observation: {
              epoch: 1n,
              lifecycle: create(LifecycleSchema, {state: {case: 'running', value: {}}}),
            },
            liveness: {epoch: 1n, value: RunLiveness.ALIVE},
            fresh: true,
          }}
          schedule={new FakeClock().schedule}
        />,
      ),
    );
    const pause = [...container.querySelectorAll('button')].find(
      button => button.textContent === 'Pause',
    );
    await act(async () => pause?.click());
    const call = bridge.calls.at(-1);
    if (call === undefined) throw new Error('Pause sent nothing');
    await act(async () =>
      call.resolve(
        toBinary(
          HostCallResponseSchema,
          create(HostCallResponseSchema, {
            result: {
              case: 'configurationFailed',
              value: {code: 'x', stage: 's', message: REASON, stderr: 'trace'},
            },
          }),
        ),
      ),
    );
    expect(container.textContent).toContain('Pause · Refused');
    expect(textNodes(container)).toContain(REASON);
    expect(container.querySelector(`[title="${REASON}"]`)).toBeNull();
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
