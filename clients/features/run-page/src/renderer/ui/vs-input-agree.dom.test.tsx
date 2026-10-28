/**
 * The `vs input` column on both faces that have it. The Ideas list and the Timeline show one idea's
 * figure, so over every fixture run they read alike: an idea's multiple, or a dash when it has none
 * (a failed check's progress is not a comparison with the input).
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {FIXTURE_NAMES, fixtureNamed} from '@vibesys/run-fixtures';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {IDEAS_FILTER_DEFAULT} from '../../state/ideas-view.js';
import {EMPTY_TRACE_LOG, traceStep} from '../../state/live.js';
import {createRunReader} from '../../state/run-model.js';
import {TIMELINE_CONTROLS_DEFAULT} from '../../state/timeline-controls.js';
import {IdeasTable} from './ideas-table.js';
import {TimelineView} from './views/timeline/timeline-view.js';

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

async function figuresOf(
  node: ReactNode,
  row: string,
  name: string,
  figure: string,
): Promise<Map<string, string>> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(node));
  const found = new Map<string, string>();
  for (const each of container.querySelectorAll(row)) {
    const label = each.querySelector(name)?.textContent ?? each.getAttribute('aria-label');
    const cell = each.querySelector(figure)?.textContent;
    if (label !== null && label !== undefined && cell !== undefined && cell !== null) {
      found.set(label, cell);
    }
  }
  await act(async () => root.unmount());
  container.remove();
  return found;
}

test('over every fixture run an idea has the same `vs input` figure on the Ideas list and the Timeline', async () => {
  let compared = 0;
  for (const name of FIXTURE_NAMES) {
    const events = fixtureNamed(name).events;
    const model = createRunReader().read(
      traceStep(EMPTY_TRACE_LOG, {kind: 'events', events}),
      null,
    );
    const base = {
      model,
      clock: {start: 0, now: null},
      tokens: EMPTY_LEDGER,
      who: new Map(),
      zoom: () => undefined,
    };
    const ideas = await figuresOf(
      <IdeasTable
        base={base}
        filter={IDEAS_FILTER_DEFAULT}
        sort="newest"
        selected={null}
        outcomes={new Map()}
        effects={new Map()}
        dispatch={() => undefined}
      />,
      '[role="option"]',
      '.vs-pickrow-text > .vs-text',
      '.vs-pickrow-figure',
    );
    const timeline = await figuresOf(
      <TimelineView
        {...base}
        idea={null}
        controls={TIMELINE_CONTROLS_DEFAULT}
        change={() => undefined}
      />,
      '[aria-label="Ideas on the timeline"] [role="option"]',
      '.vs-gantt-name',
      '.vs-gantt-figure',
    );
    for (const [title, cell] of timeline) {
      const listed = ideas.get(title);
      if (listed === undefined) continue;
      compared += 1;
      expect({fixture: name, title, cell}).toEqual({fixture: name, title, cell: listed});
    }
  }
  expect(compared).toBeGreaterThan(10);
});
