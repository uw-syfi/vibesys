/** The "Start a run" sheet in a browser DOM: the model picker and what Start then asks for. */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {HarnessListing, WelcomeResult} from '@vibesys/ipc';
import {Window} from 'happy-dom';
import {act} from 'react';
import type {Root} from 'react-dom/client';
import {
  START_CLOSED,
  type StartAction,
  type StartRequest,
  type StartState,
  startStep,
} from '../../state/start.js';
import {type ProjectField, StartSheet} from './start-sheet.js';

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

const LISTED: WelcomeResult<HarnessListing> = {
  ok: true,
  value: {
    kind: 'listed',
    harnesses: [
      {provider: 'codex', model: 'gpt-a', default: true},
      {provider: 'codex', model: 'gpt-b', default: false},
      {provider: 'claude', model: 'claude-a', default: true},
    ],
  },
};

const FIELD: ProjectField = props => <input readOnly value={props.value} />;

interface MountedSheet {
  readonly container: HTMLElement;
  readonly root: Root;
  readonly requests: StartRequest[];
  dispatch(action: StartAction): Promise<void>;
}

/** The sheet opened on one project, its harness and task replies answered with `harnesses`. */
async function mountSheet(harnesses: WelcomeResult<HarnessListing>): Promise<MountedSheet> {
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const requests: StartRequest[] = [];
  let state: StartState = START_CLOSED;
  const render = (): void =>
    root.render(<StartSheet state={state} visible projectField={FIELD} start={start} />);
  const start = (action: StartAction): void => {
    const next = startStep(state, action);
    state = next.state;
    requests.push(...next.requests);
    render();
  };
  await act(async () =>
    start({type: 'open', host: {key: 'local', label: 'This Mac'}, projects: ['/p']}),
  );
  const token = (kind: StartRequest['kind']): number =>
    requests.find(request => request.kind === kind)?.token ?? 0;
  await act(async () => {
    start({type: 'harnesses-reply', token: token('harnesses'), result: harnesses});
    start({type: 'tasks-reply', token: token('tasks'), result: {ok: true, value: ['spsc']}});
  });
  return {container, root, requests, dispatch: async action => act(async () => start(action))};
}

async function unmount(mounted: MountedSheet): Promise<void> {
  await act(async () => mounted.root.unmount());
  mounted.container.remove();
}

function picker(container: HTMLElement): HTMLSelectElement | null {
  return container.querySelector<HTMLSelectElement>('select[aria-label="Model"]');
}

function startButton(container: HTMLElement): HTMLButtonElement {
  const button = [...container.querySelectorAll('button')].find(
    candidate => candidate.textContent === 'Start run',
  );
  if (button === undefined) throw new Error('the sheet has no Start run button');
  return button;
}

test('the picker offers the project default and each vetted model, defaults marked', async () => {
  const mounted = await mountSheet(LISTED);
  const select = picker(mounted.container);
  expect(select).not.toBeNull();
  const options = [...(select?.querySelectorAll('option') ?? [])].map(option => option.textContent);
  expect(options).toEqual([
    'Project default (agent.toml)',
    'gpt-a (default)',
    'gpt-b',
    'claude-a (default)',
  ]);
  const groups = [...(select?.querySelectorAll('optgroup') ?? [])].map(group => group.label);
  expect(groups).toEqual(['codex', 'claude']);
  expect(select?.value).toBe('');
  // There is no free-text model field anywhere on the sheet.
  const inputs = [...mounted.container.querySelectorAll('input:not([type="radio"])')];
  expect(inputs.every(input => input.getAttribute('aria-label') !== 'Model')).toBeTrue();
  await unmount(mounted);
});

test('choosing a model sends that pair with the start; the summary names it', async () => {
  const mounted = await mountSheet(LISTED);
  const select = picker(mounted.container);
  if (select === null) throw new Error('no model picker');
  await act(async () => {
    select.value = '1';
    select.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
  });
  expect(mounted.container.textContent).toContain('--cli-provider codex --model gpt-b');
  await act(async () => startButton(mounted.container).click());
  const starts = mounted.requests.filter(request => request.kind === 'start');
  expect(starts).toMatchObject([{task: 'spsc', harness: {provider: 'codex', model: 'gpt-b'}}]);
  await unmount(mounted);
});

test('a host whose helper cannot take a choice shows no picker and starts with none', async () => {
  const mounted = await mountSheet({ok: true, value: {kind: 'unsupported'}});
  expect(picker(mounted.container)).toBeNull();
  await act(async () => startButton(mounted.container).click());
  const starts = mounted.requests.filter(request => request.kind === 'start');
  expect(starts).toMatchObject([{task: 'spsc', harness: null}]);
  await unmount(mounted);
});
