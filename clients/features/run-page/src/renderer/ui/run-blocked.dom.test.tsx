/**
 * A run on a machine that will not answer (its VibeSys needs an update, it needs a sign-in, or it
 * cannot be reached) says which, in words. Regression: the Run screen stayed on "Finding the run…"
 * for good while the sidebar said the machine could not be reached, and Retry did nothing.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  RUN_ROUTE,
  type RunPageState,
  type RunRecord,
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

const BLOCKS = [
  ['needs-update', 'VibeSys on host-b needs an update', 'Needs an update'],
  ['sign-in', 'Sign in to host-b', 'Sign in'],
  ['offline', 'Can’t reach host-b', 'Unreachable'],
  ['closed', 'Disconnected from host-b', 'Disconnected'],
] as const;

function blocked(record: RunRecord): RunPageState {
  return runPageStep(
    runPageInitial({
      identity: {host: 'ssh:host-b', runId: 'run-1'},
      title: {project: 'queue-rs', task: null},
      hostLabel: 'host-b',
      unavailable: {},
    }),
    {type: 'record', record},
  ).state;
}

async function screen(state: RunPageState): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  const model = createRunReader().read(state.run.trace, null);
  await act(async () =>
    createRoot(container).render(
      <RunScreen
        state={state}
        model={model}
        whole={model}
        core={state.run.core}
        route={RUN_ROUTE}
        zoom={() => undefined}
        dispatch={() => undefined}
      />,
    ),
  );
  return container;
}

for (const [block, words, status] of BLOCKS) {
  test(`a run on a machine that is ${block} says so, in words, with no endless search and no Retry`, async () => {
    const container = await screen(blocked({kind: 'unlisted', why: 'machine', block}));
    expect(container.textContent).toContain(words);
    expect(container.textContent).toContain(status);
    expect(container.textContent).not.toContain('Finding the run');
    expect(container.textContent).not.toContain('Retry');
  });
}

test('a run on a machine still connecting is still being found', async () => {
  const container = await screen(blocked({kind: 'unlisted', why: 'machine'}));
  expect(container.textContent).toContain('Finding the run');
});
