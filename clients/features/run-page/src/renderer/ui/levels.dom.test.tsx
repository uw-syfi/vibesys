/**
 * The idea level in a DOM: its header says why an idea failed, in the words of the Ideas list.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {type RunModel, runPageInitial} from '../../state/index.js';
import {idea, modelOf} from '../../state/timeline-fixtures.js';
import {DeepLevel} from './levels.js';

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

/** The chips of the opened idea's header. */
async function chipsOf(
  outcome: 'passed' | 'failed' | 'running',
  reason: string | null,
): Promise<(string | null)[]> {
  const opened = {...idea('mpmc', outcome, 100, 300), reason};
  const model = {
    ...modelOf([opened]),
    overview: null,
    changed: new Set<string>(),
    agents: {agents: []},
    trace: {spans: new Map()},
  } as unknown as RunModel;
  const state = runPageInitial({
    identity: {host: 'ssh:chelan4', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'chelan4',
    unavailable: {},
  });
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <DeepLevel
        state={state}
        base={{model, clock: {start: 0, now: 1000}, zoom: () => undefined}}
        core={null}
        mode="ideas"
        dispatch={() => undefined}
        zoomed={{idea: opened, agent: null, step: null, item: null}}
      />,
    ),
  );
  const chips = [...container.querySelectorAll('.vs-chip')].map(chip => chip.textContent);
  await act(async () => root.unmount());
  container.remove();
  return chips;
}

test('a failed idea names why in its header, as the Ideas list does', async () => {
  for (const [reason, said] of [
    ['turned_down', 'Failed · turned down'],
    ['not_measured', 'Failed · not measured'],
    ['no_design', 'Failed · no design'],
    ['not_opened', 'Failed · not opened'],
  ] as const) {
    expect(await chipsOf('failed', reason)).toContain(said);
  }
  expect(await chipsOf('failed', null)).toContain('Failed');
});

test('a passed or running idea carries no reason in its header', async () => {
  expect(await chipsOf('passed', null)).toContain('Passed');
  expect(await chipsOf('running', null)).toContain('Running');
});
