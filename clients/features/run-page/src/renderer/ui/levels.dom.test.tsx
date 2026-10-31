/**
 * The idea level in a DOM: its header says why an idea failed, in the words of the Ideas list.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {type RunModel, runPageInitial} from '../../state/index.js';
import {idea, modelOf, step} from '../../state/timeline-fixtures.js';
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

/** The chips of the opened idea's header and the text of its whole pane. */
async function paneOf(
  outcome: 'passed' | 'failed' | 'running',
  reason: string | null,
): Promise<{chips: (string | null)[]; text: string}> {
  const opened = {...idea('mpmc', outcome, 100, 300), reason};
  const model = {
    ...modelOf([opened]),
    overview: null,
    inputGates: [],
    changed: new Set<string>(),
    agents: {agents: []},
    trace: {spans: new Map()},
  } as unknown as RunModel;
  const state = runPageInitial({
    identity: {host: 'ssh:host-b', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-b',
    unavailable: {},
  });
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <DeepLevel
        state={state}
        base={{
          model,
          clock: {start: 0, now: 1000},
          tokens: EMPTY_LEDGER,
          who: new Map(),
          zoom: () => undefined,
        }}
        core={null}
        mode="ideas"
        dispatch={() => undefined}
        loading={null}
        problem={null}
        zoomed={{idea: opened, agent: null, step: null, item: null}}
      />,
    ),
  );
  const chips = [...container.querySelectorAll('.vs-chip')].map(chip => chip.textContent);
  const pane = [...container.querySelectorAll('section.vs-pane')].at(-1);
  const text = pane?.textContent ?? '';
  await act(async () => root.unmount());
  container.remove();
  return {chips, text};
}

const chipsOf = async (outcome: 'passed' | 'failed' | 'running', reason: string | null) =>
  (await paneOf(outcome, reason)).chips;

test('a failed idea says why once: its badge says Failed and its body line gives the reason', async () => {
  for (const [reason, said] of [
    ['turned_down', 'Not measured'],
    ['not_measured', 'Not measured'],
    ['no_design', 'No design'],
    ['not_opened', 'Not opened'],
  ] as const) {
    const {chips, text} = await paneOf('failed', reason);
    expect(chips).toContain('Failed');
    expect(text).toContain(said);
    expect(text.match(new RegExp(said, 'gi'))).toHaveLength(1);
    expect(text).not.toMatch(/Failed · /);
  }
});

test('a passed or running idea carries no reason in its header', async () => {
  expect(await chipsOf('passed', null)).toContain('Passed');
  expect(await chipsOf('running', null)).toContain('Running');
});

/** The chips of an opened step's header, with the text each shows and the pointer it carries. */
async function stepChips(problem: string | null): Promise<{text: string | null; hint: string}[]> {
  const opened = idea('mpmc', 'passed', 100, 300);
  const model = {
    ...modelOf([opened]),
    overview: null,
    inputGates: [],
    changed: new Set<string>(),
    agents: {agents: []},
    trace: {spans: new Map()},
  } as unknown as RunModel;
  const state = runPageInitial({
    identity: {host: 'ssh:host-b', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-b',
    unavailable: {},
  });
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <DeepLevel
        state={state}
        base={{
          model,
          clock: {start: 0, now: 1000},
          tokens: EMPTY_LEDGER,
          who: new Map(),
          zoom: () => undefined,
        }}
        core={null}
        mode="ideas"
        dispatch={() => undefined}
        loading={null}
        problem={problem}
        zoomed={{
          idea: opened,
          agent: null,
          step: step('s1', 'implement', 'implementer', 100, 50),
          item: null,
        }}
      />,
    ),
  );
  const chips = [...container.querySelectorAll('.vs-chip')].map(chip => ({
    text: chip.textContent,
    hint: chip.getAttribute('aria-description') ?? '',
  }));
  await act(async () => root.unmount());
  container.remove();
  return chips;
}

test('a step whose transcript could not be read says so at the step, with the reason as its description', async () => {
  const chips = await stepChips('the span page made no progress');
  expect(chips).toContainEqual({
    text: 'Transcript unavailable',
    hint: 'the span page made no progress',
  });
  expect((await stepChips(null)).map(chip => chip.text)).not.toContain('Transcript unavailable');
});
