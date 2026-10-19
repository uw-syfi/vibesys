/**
 * A run its host's listing does not place ends its wait in a status word and the reason in a few
 * words, never in "Finding the run…" for ever. Regression: an older finished run opened from the
 * host's run list showed the spinner without end.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  RUN_ROUTE,
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

function opened(why: 'machine' | 'unconfirmed'): RunPageState {
  const first = runPageStep(
    runPageInitial({
      identity: {host: 'ssh:host-a', runId: 'run-1'},
      title: {project: 'queue-rs', task: null},
      hostLabel: 'host-a',
      unavailable: {},
    }),
    {type: 'locate'},
  );
  return runPageStep(first.state, {type: 'record', record: {kind: 'unlisted', why}}).state;
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

for (const why of ['machine', 'unconfirmed'] as const) {
  test(`an unplaced run (${why}) shows a status word and its reason once the wait is bounded out`, async () => {
    const waiting = opened(why);
    expect((await screen(waiting)).textContent).toContain('Finding the run');
    const bound = waiting.locate.kind === 'waiting' ? waiting.locate.bound : -1;
    const settled = runPageStep(waiting, {type: 'bound-elapsed', token: bound}).state;
    const text = (await screen(settled)).textContent ?? '';
    expect(text).not.toContain('Finding the run');
    expect(text).not.toContain('Looking for the run');
    expect(text).toContain(
      why === 'machine' ? 'No answer · runs not listed' : 'Not confirmed · live state unknown',
    );
  });
}
