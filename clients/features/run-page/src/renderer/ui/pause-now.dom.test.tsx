/**
 * Pause now stops work that may still be charged, so the page asks first: a dialog that names what
 * it costs, with Keep running and Pause now. It is a question over the page, not a strip in it.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {HostRun} from '@vibesys/hosts';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  RUN_ROUTE,
  type RunPageAction,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from '../../state/index.js';
import {RunScreen} from './run-screen.js';

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

const RUNNING: HostRun = {
  id: 'a1b2c3d4e5f6',
  projectRoot: '~/vibesys-projects/queue-rs',
  runId: 'run-1',
  status: 'running',
  phase: 'running',
  paused: null,
  end: null,
  startedAt: 1,
  reason: null,
};

function live(): RunPageState {
  const state = runPageStep(
    runPageInitial({
      identity: {host: 'ssh:host-b', runId: 'run-1'},
      title: {project: 'queue-rs', task: null},
      hostLabel: 'host-b',
      unavailable: {},
    }),
    {type: 'record', record: {kind: 'live', run: RUNNING}},
  ).state;
  return runPageStep(state, {type: 'ask-pause-now'}).state;
}

async function screen(
  state: RunPageState,
  dispatch: (action: RunPageAction) => void,
): Promise<{container: HTMLElement; unmount: () => Promise<void>}> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const model = createRunReader().read(state.run.trace, null);
  await act(async () =>
    root.render(
      <RunScreen
        state={state}
        model={model}
        whole={model}
        core={null}
        route={RUN_ROUTE}
        zoom={() => undefined}
        dispatch={dispatch}
      />,
    ),
  );
  return {
    container,
    unmount: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

const named = (container: HTMLElement, text: string): HTMLElement | undefined =>
  [...container.querySelectorAll<HTMLElement>('dialog button')].find(
    button => button.textContent === text,
  );

test('a confirming Pause now is a dialog that names its cost, answered by Keep running or Pause now', async () => {
  const asked: RunPageAction[] = [];
  const {container, unmount} = await screen(live(), action => asked.push(action));
  const dialog = container.querySelector('dialog');
  expect(dialog?.hasAttribute('open')).toBe(true);
  expect(dialog?.textContent).toContain('Pause now?');
  expect(dialog?.textContent).toContain('Unfinished work may need repeating');
  await act(async () => named(container, 'Keep running')?.click());
  await act(async () => named(container, 'Pause now')?.click());
  expect(asked).toEqual([{type: 'cancel-pause-now'}, {type: 'confirm-pause-now'}]);
  await unmount();
});

test('with nothing to confirm there is no open dialog and no strip', async () => {
  const idle = runPageStep(live(), {type: 'cancel-pause-now'}).state;
  const {container, unmount} = await screen(idle, () => undefined);
  expect(container.querySelector('dialog[open]')).toBeNull();
  expect(container.textContent).not.toContain('Unfinished work may need repeating');
  await unmount();
});
