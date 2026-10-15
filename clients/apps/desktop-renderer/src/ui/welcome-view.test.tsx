/**
 * The welcome view in a DOM (happy-dom): what only a live React tree shows, such as whether an
 * element survives a re-render.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {
  COMPLETION_EMPTY,
  type CompletionAction,
  completionStep,
  hostInitial,
  INSPECTOR_SHEET_CLOSED,
  type InspectorSheetAction,
  PICKER_CLOSED,
} from '@vibesys/feature-hosts/state';
import {LISTS_EMPTY} from '@vibesys/feature-recent-runs/state';
import {WELCOME_INITIAL} from '@vibesys/feature-run-control/state';
import {START_CLOSED, type StartAction, startStep} from '@vibesys/feature-start-run/state';
import type {HostListing} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {Dialog} from '@vibesys/ui';
import {Window} from 'happy-dom';
import {act} from 'react';
import type {Root} from 'react-dom/client';
import {WelcomeView, type WelcomeViewProps} from './welcome-view.js';

const window = new Window();
// The globals happy-dom's window has and the test runtime lacks (`document`, `HTMLElement`,
// `Event`, ...), installed for React DOM and removed after the file's tests.
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

/**
 * The view over pure folds of its start and completion states, re-rendered after every event it
 * reports, as the stores and `useSyncExternalStore` re-render it in the app.
 */
async function mountWelcome(): Promise<{readonly root: Root; readonly container: HTMLElement}> {
  // React DOM reads the DOM globals when it loads, so it loads after they are installed.
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const opened = startStep(START_CLOSED, {
    type: 'open',
    host: {key: 'local', label: 'This Mac'},
    projects: ['/home/me/src/queue-rs'],
  }).state;
  let props: WelcomeViewProps;
  const render = (): void => root.render(<WelcomeView {...props} />);
  const start = (action: StartAction): void => {
    props = {...props, start: startStep(props.start, action).state};
    render();
  };
  const completion = (action: CompletionAction): void => {
    props = {...props, completion: completionStep(props.completion, action).state};
    render();
  };
  props = {
    welcome: WELCOME_INITIAL,
    lists: LISTS_EMPTY,
    host: hostInitial('local'),
    start: opened,
    picker: PICKER_CLOSED,
    completion: COMPLETION_EMPTY,
    inspector: INSPECTOR_SHEET_CLOSED,
    send: {
      welcome: () => undefined,
      host: () => undefined,
      start,
      picker: () => undefined,
      completion,
      inspector: () => undefined,
    },
  };
  await act(async () => render());
  return {root, container};
}

function projectInput(container: HTMLElement): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>(
    'input[placeholder^="Project directory"]',
  );
  if (input === null) throw new Error('the start sheet shows no project field');
  return input;
}

/** Type `text` into `input` as the browser reports it: the value changes, then `input` fires. */
async function type(input: HTMLInputElement, text: string): Promise<void> {
  const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  if (setValue === undefined) throw new Error('happy-dom inputs have no value setter');
  await act(async () => {
    setValue.call(input, text);
    input.dispatchEvent(new window.Event('input', {bubbles: true}) as unknown as Event);
  });
}

test('typing in the start sheet keeps the project field the same element', async () => {
  const {root, container} = await mountWelcome();
  const field = projectInput(container);
  field.focus();
  for (const typed of ['/', '/h', '/ho', '/home', '/home/me']) {
    await type(projectInput(container), typed);
    // A field remounted on every render loses the caret and the focus after each keystroke, and
    // the next keystroke's completion request remounts it again.
    expect(projectInput(container)).toBe(field);
    expect(field.value).toBe(typed);
    expect(document.activeElement).toBe(field);
  }
  await act(async () => root.unmount());
  container.remove();
});

test('the public dialog restores focus to the opener or its stable fallback', async () => {
  const {createRoot} = await import('react-dom/client');
  for (const removeOpener of [false, true]) {
    const opener = document.createElement('button');
    const fallback = document.createElement('section');
    const container = document.createElement('div');
    fallback.id = 'dialog-focus-fallback';
    fallback.tabIndex = -1;
    document.body.append(opener, fallback, container);
    opener.focus();
    const root = createRoot(container);
    const render = (open: boolean): void =>
      root.render(
        <Dialog
          open={open}
          label="Focus owner"
          focusFallbackId={fallback.id}
          dismiss={() => undefined}
        >
          <button type="button">Inside</button>
        </Dialog>,
      );

    await act(async () => render(true));
    if (removeOpener) opener.remove();
    await act(async () => render(false));

    expect(document.activeElement).toBe(removeOpener ? fallback : opener);
    await act(async () => root.unmount());
    opener.remove();
    fallback.remove();
    container.remove();
  }
});

test('a retained recent locator stays inspectable while its host listing is unavailable', async () => {
  const {createRoot} = await import('react-dom/client');
  const instanceId = '0123456789ab';
  const recent: RecentRun = {
    host: 'ssh:gpu',
    hostLabel: 'gpu',
    project: '/projects/queue',
    task: 'latency',
    runId: 'run-retained',
    instanceId,
    attachedAt: 1,
  };
  const unavailable: HostListing = {
    kind: 'unavailable',
    host: recent.host,
    connection: {kind: 'reconnecting', attempt: 2},
  };
  for (const listing of [undefined, unavailable]) {
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    const inspections: InspectorSheetAction[] = [];
    const props: WelcomeViewProps = {
      welcome: WELCOME_INITIAL,
      lists: {
        runs: [recent],
        listings: listing === undefined ? {} : {[recent.host]: listing},
      },
      host: hostInitial('local'),
      start: START_CLOSED,
      picker: PICKER_CLOSED,
      completion: COMPLETION_EMPTY,
      inspector: INSPECTOR_SHEET_CLOSED,
      send: {
        welcome: () => undefined,
        host: () => undefined,
        start: () => undefined,
        picker: () => undefined,
        completion: () => undefined,
        inspector: action => inspections.push(action),
      },
    };
    await act(async () => root.render(<WelcomeView {...props} />));

    const row = [...container.querySelectorAll('tr')].find(entry =>
      entry.textContent?.includes(recent.runId),
    );
    const inspect = [...(row?.querySelectorAll('button') ?? [])].find(
      button => button.textContent === 'Inspect',
    );
    expect(inspect).toBeInstanceOf(window.HTMLButtonElement);
    if (inspect === undefined) throw new Error('the recent row has no Inspect button');
    inspect.focus();
    expect(document.activeElement).toBe(inspect);
    await act(async () => inspect.click());
    expect(inspections).toEqual([
      {
        type: 'open',
        target: {
          host: recent.host,
          hostLabel: recent.hostLabel,
          instanceId,
          task: recent.task,
          runId: recent.runId,
          project: recent.project,
          focusFallbackId: 'recent-section',
        },
      },
    ]);

    await act(async () => root.unmount());
    container.remove();
  }
});
