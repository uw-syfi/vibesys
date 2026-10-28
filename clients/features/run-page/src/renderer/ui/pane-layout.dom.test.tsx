/**
 * The Run screen's panes over the live `queue-rs-live` run: an idea opened from the Timeline sits
 * beside the Timeline, and each list beside what it opened resizes, its width going to its role.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {EMPTY_LEDGER, initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import type {Candidate} from '@vibesys/run-state/candidates';
import {Window} from 'happy-dom';
import {act, type JSX} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  type PaneRole,
  type PaneSizes,
  type PaneWidths,
  type RunModel,
  runPageInitial,
  type ZoomEvent,
} from '../../state/index.js';
import {EMPTY_TRACE_LOG, traceStep} from '../../state/live.js';
import {IdeasWorkspace} from './levels.js';
import {RunLevel} from './run-level.js';
import type {ViewBase} from './views/props.js';

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

const EVENTS = fixtureNamed('queue-rs-live').events;
const MODEL: RunModel = createRunReader().read(
  traceStep(EMPTY_TRACE_LOG, {kind: 'events', events: EVENTS}),
  null,
);
const CORE = reduceEventBatch(initialCoreState(), EVENTS);
const STATE = runPageInitial({
  identity: {host: 'ssh:host-b', runId: 'run-1'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'host-b',
  unavailable: {},
});
const IDEA = MODEL.candidates.candidates.find(
  item => item.title === 'Wait-free consumer path',
) as Candidate;

function base(zooms: ZoomEvent[]): ViewBase {
  return {
    model: MODEL,
    clock: {start: 0, now: null},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: event => zooms.push(event),
  };
}

/** The widths a page holds, recording every change by role. */
function sizes(widths: PaneWidths, changes: [PaneRole, number][]): PaneSizes {
  return {widths, change: (role, width) => changes.push([role, width])};
}

async function rendered(
  element: JSX.Element,
): Promise<{container: HTMLElement; done: () => Promise<void>}> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(element));
  return {
    container,
    done: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

async function press(element: Element, key: string): Promise<void> {
  await act(async () => {
    (element as HTMLElement).focus();
    element.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true}));
  });
}

function separator(container: HTMLElement, name: string): HTMLElement {
  const found = container.querySelector<HTMLElement>(`hr[aria-label="${name}"]`);
  if (found === null) throw new Error(`no separator ${name}`);
  return found;
}

test('an idea opened from the Timeline sits beside it, its row selected, and closes back to it', async () => {
  const zooms: ZoomEvent[] = [];
  const screen = await rendered(
    <IdeasWorkspace
      state={STATE}
      base={base(zooms)}
      core={CORE}
      mode="timeline"
      idea={IDEA}
      dispatch={() => undefined}
      loading={null}
    />,
  );
  const {container} = screen;
  const gantt = container.querySelector('[aria-label="Ideas on the timeline"]');
  expect(gantt).not.toBeNull();
  const selected = [...(gantt?.querySelectorAll('[aria-selected="true"]') ?? [])];
  expect(selected.map(row => row.getAttribute('aria-label'))).toEqual(['Wait-free consumer path']);
  const panes = [...container.querySelectorAll('section.vs-pane')];
  expect(panes.map(pane => pane.getAttribute('data-width'))).toEqual(['wide', 'fill']);
  expect(panes[1]?.querySelector('h2')?.textContent).toBe('Wait-free consumer path');
  // One face switch, on the Timeline: the idea beside it has no Steps and Logs of its own.
  const switches = [...container.querySelectorAll('[role="radiogroup"][aria-label="Show"]')];
  expect(switches).toHaveLength(1);
  expect(panes[0]?.contains(switches[0] ?? null)).toBe(true);
  const close = panes[1]?.querySelector('button[aria-label="Close idea"]');
  await act(async () => {
    close?.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
  });
  expect(zooms).toEqual([{type: 'up'}]);
  await screen.done();
});

test('each list beside what it opened resizes, and the new width goes to its kind of pane', async () => {
  const cases = [
    {mode: 'ideas', idea: IDEA, name: 'Resize the ideas list', key: 'ArrowRight', role: 'master'},
    {mode: 'timeline', idea: IDEA, name: 'Resize the timeline', key: 'ArrowRight', role: 'wide'},
    // Now sits at the right of a run-level face: Left widens it.
    {mode: 'agents', idea: null, name: 'Resize the running list', key: 'ArrowLeft', role: 'list'},
  ] as const;
  for (const each of cases) {
    const changes: [PaneRole, number][] = [];
    const props = {
      state: STATE,
      base: base([]),
      core: CORE,
      mode: each.mode,
      dispatch: () => undefined,
      panes: sizes({[each.role]: 500}, changes),
    };
    const screen = await rendered(
      each.idea === null ? (
        <RunLevel {...props} />
      ) : (
        <IdeasWorkspace {...props} idea={each.idea} loading={null} />
      ),
    );
    const handle = separator(screen.container, each.name);
    const pane = screen.container.querySelector<HTMLElement>(
      `#${CSS.escape(handle.getAttribute('aria-controls') ?? '')}`,
    );
    expect(pane?.style.getPropertyValue('--pane-size')).toBe('500px');
    await press(handle, each.key);
    expect(changes).toEqual([[each.role, 516]]);
    expect(screen.container.querySelectorAll('hr[aria-orientation="vertical"]')).toHaveLength(1);
    await screen.done();
  }
});

test('without the page’s widths no pane resizes', async () => {
  const screen = await rendered(
    <IdeasWorkspace
      state={STATE}
      base={base([])}
      core={CORE}
      mode="ideas"
      idea={IDEA}
      dispatch={() => undefined}
      loading={null}
    />,
  );
  expect(screen.container.querySelector('hr')).toBeNull();
  await screen.done();
});
