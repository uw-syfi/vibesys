/**
 * A run its host holds no record of (the sidebar says "this run is gone") has nothing to retry:
 * asking again finds no run. Regression: the Run screen showed "Could not load this run" with a
 * Retry button for it.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {type HostRun, RUN_GONE_WORDS} from '@vibesys/hosts';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  RUN_ROUTE,
  type RunPageRequest,
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

const ENDED: HostRun = {
  id: 'a1b2c3d4e5f6',
  projectRoot: '~/vibesys-projects/queue-rs',
  runId: 'run-1',
  status: 'stopped',
  phase: 'stopped',
  paused: null,
  end: null,
  startedAt: 1,
  reason: null,
};

const RECORDS = [
  {kind: 'ended', run: ENDED},
  {kind: 'historical', status: null},
] as const;

function goneRun(record: (typeof RECORDS)[number]): {
  readonly state: RunPageState;
  readonly requests: readonly RunPageRequest[];
} {
  const opened = runPageStep(
    runPageInitial({
      identity: {host: 'ssh:host-b', runId: 'run-1'},
      title: {project: 'queue-rs', task: null},
      hostLabel: 'host-b',
      unavailable: {},
    }),
    {type: 'record', record},
  );
  const asked = opened.requests.find(request => request.kind === 'read-summary');
  if (asked?.kind !== 'read-summary') throw new Error('no summary asked');
  return runPageStep(opened.state, {
    type: 'summary',
    token: asked.token,
    reply: {kind: 'gone'},
    wallMs: 0,
  });
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

for (const record of RECORDS) {
  test(`a ${record.kind} run the host has no record of says so in the sidebar's words, with no Retry`, async () => {
    const {state, requests} = goneRun(record);

    expect(requests).toEqual([]);
    const container = await screen(state);
    expect(container.textContent).toContain(RUN_GONE_WORDS);
    expect(container.textContent).not.toContain('No ideas yet');
    expect(container.textContent).not.toContain('Retry');
  });
}
