/** The startup log panel as the run page renders it, in a DOM, from views a test chooses. */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {type CoreState, initialCoreState, initialIterationNavigation} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act} from 'react';
import type {Root} from 'react-dom/client';
import type {StartupLogEvent, StartupLogView} from '../state/index.js';
import {RunPage} from './index.js';

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
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

const STARTING: CoreState = {...initialCoreState(), status: 'starting'};

const SHOWING: StartupLogView = {
  visible: true,
  expanded: true,
  listing: 'loaded',
  listFailure: null,
  streams: [
    {id: '000001', stage: 'building_image', outcome: 'ended'},
    {id: '000002', stage: 'starting_container', outcome: null},
  ],
  shown: '000002',
  lines: ['pulling layer', 'starting container'],
  earlier: false,
  readFailure: null,
};

async function mount(
  view: StartupLogView,
  sent: StartupLogEvent[],
): Promise<{root: Root; container: HTMLElement}> {
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <RunPage
        run={STARTING}
        iterations={initialIterationNavigation()}
        session={null}
        replayFailure={null}
        onReattach={() => undefined}
        onReconnectControls={() => undefined}
        onRetryReplay={() => undefined}
        onCommand={null}
        now={0}
        startupLog={{view, send: event => sent.push(event)}}
      />,
    ),
  );
  return {root, container};
}

test('a starting run shows its startup output in monospace, and lets the user pick a step', async () => {
  const sent: StartupLogEvent[] = [];
  const {root, container} = await mount(SHOWING, sent);
  const panel = container.querySelector('[data-testid="startup-log"]');
  expect(panel?.querySelector('pre')?.textContent).toBe('pulling layer\nstarting container');

  const select = panel?.querySelector('select');
  if (select == null) throw new Error('the step picker is missing');
  await act(async () => {
    select.value = '000001';
    select.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
  });
  expect(sent).toContainEqual({type: 'select', streamId: '000001'});

  const toggle = panel?.querySelector('button');
  await act(async () => toggle?.click());
  expect(sent).toContainEqual({type: 'collapse'});
  await act(async () => root.unmount());
  container.remove();
});

test('an unsupported host shows its notice, and a hidden panel renders nothing', async () => {
  const unsupported = await mount(
    {...SHOWING, listing: 'unsupported', streams: [], shown: null, lines: []},
    [],
  );
  expect(unsupported.container.textContent).toContain('does not serve logs');
  await act(async () => unsupported.root.unmount());
  unsupported.container.remove();

  const hidden = await mount({...SHOWING, visible: false}, []);
  expect(hidden.container.querySelector('[data-testid="startup-log"]')).toBeNull();
  await act(async () => hidden.root.unmount());
  hidden.container.remove();
});
