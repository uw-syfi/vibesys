/**
 * A `Gantt` of rows that nest: a fold opens and closes by its chevron and by Right and Left without
 * pressing the row, a press opens the row, tags, counts and marks are drawn, and only a row that
 * arrives while the Gantt is on screen draws in.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {Gantt, type GanttRow} from './index.js';

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

const row = (id: string, extra: Partial<GanttRow> = {}): GanttRow => ({
  id,
  label: id,
  start: 0,
  end: 10,
  outcome: 'muted',
  segments: [{id, start: 0, end: 10, kind: 'grey', label: id}],
  press: () => undefined,
  ...extra,
});

function show(root: ReturnType<typeof createRoot>, rows: readonly GanttRow[]) {
  return act(async () =>
    root.render(<Gantt label="Agents" from={0} to={10} rows={rows} tagLabel="Machine" />),
  );
}

function showPrimary(root: ReturnType<typeof createRoot>, rows: readonly GanttRow[]) {
  return act(async () =>
    root.render(
      <Gantt
        label="Agents"
        from={0}
        to={10}
        rows={rows}
        tagLabel="Runtime"
        tagPriority="primary"
      />,
    ),
  );
}

async function mounted(rows: readonly GanttRow[]) {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await show(root, rows);
  return {container, root};
}

const options = (container: HTMLElement) => [...container.querySelectorAll('[role="option"]')];

describe('a Gantt of nested rows', () => {
  test('a press on the chevron toggles the fold and does not press the row', async () => {
    const log: string[] = [];
    const {container} = await mounted([
      row('lead', {
        press: () => log.push('press'),
        fold: {open: true, toggle: () => log.push('toggle')},
      }),
    ]);
    const chevron = container.querySelector('.vs-gantt-fold') as HTMLElement;
    await act(async () => chevron.click());
    expect(log).toEqual(['toggle']);
    await act(async () => (options(container)[0] as HTMLElement).click());
    expect(log).toEqual(['toggle', 'press']);
  });

  test('Right opens a closed fold and Left closes an open one, and neither presses the row', async () => {
    const log: string[] = [];
    const key = (target: Element, name: string) =>
      act(async () =>
        target.dispatchEvent(
          new KeyboardEvent('keydown', {key: name, bubbles: true, cancelable: true}),
        ),
      );
    const closed = await mounted([
      row('a', {
        press: () => log.push('press'),
        fold: {open: false, toggle: () => log.push('open')},
      }),
    ]);
    const open = await mounted([
      row('b', {
        press: () => log.push('press'),
        fold: {open: true, toggle: () => log.push('close')},
      }),
    ]);
    await key(options(closed.container)[0] as Element, 'ArrowLeft');
    await key(options(closed.container)[0] as Element, 'ArrowRight');
    await key(options(open.container)[0] as Element, 'ArrowRight');
    await key(options(open.container)[0] as Element, 'ArrowLeft');
    expect(log).toEqual(['open', 'close']);
  });

  test('every row keeps the same columns at any depth, and names its fold and count', async () => {
    const {container} = await mounted([
      row('lead', {fold: {open: true, toggle: () => undefined}, tag: 'host-a'}),
      row('worker', {
        guides: ['elbow'],
        count: 12,
        tag: 'host-a',
        fold: {open: false, toggle: () => undefined},
      }),
      row('deep', {guides: ['pipe', 'elbow'], tag: 'host-a'}),
    ]);
    const rows = options(container);
    expect(rows.map(one => one.getAttribute('aria-label'))).toEqual([
      'lead, open',
      'worker ×12, folded',
      'deep',
    ]);
    for (const one of rows) {
      expect(one.children).toHaveLength(4);
      expect(one.querySelector('.vs-gantt-tag')?.textContent).toBe('host-a');
    }
    expect(rows[1]?.querySelector('.vs-gantt-count')?.textContent).toBe('×12');
    expect(rows[1]?.querySelector('.vs-gantt-bar')?.hasAttribute('data-stacked')).toBe(true);
    expect(rows[0]?.querySelector('.vs-gantt-bar')?.hasAttribute('data-stacked')).toBe(false);
  });

  test('marks a primary tag so responsive layout keeps row identity visible', async () => {
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    await showPrimary(root, [row('lead', {tag: 'model-a on Codex'})]);
    expect(container.querySelector('.vs-gantt')?.getAttribute('data-tag-priority')).toBe('primary');
    expect(options(container)[0]?.querySelector('.vs-gantt-tag')?.textContent).toBe(
      'model-a on Codex',
    );
  });

  test('a count on an open row shows its number and no stack', async () => {
    const {container} = await mounted([
      row('group', {count: 7, fold: {open: true, toggle: () => undefined}}),
    ]);
    expect(container.querySelector('.vs-gantt-count')?.textContent).toBe('×7');
    expect(container.querySelector('.vs-gantt-bar')?.hasAttribute('data-stacked')).toBe(false);
  });

  test('marks are drawn on the bar at their moments, in their tone', async () => {
    const {container} = await mounted([
      row('lead', {
        marks: [
          {at: 2.5, tone: 'ok', label: 'worker-1'},
          {at: 10, tone: 'err', label: 'worker-2'},
        ],
      }),
    ]);
    const marks = [...container.querySelectorAll('.vs-gantt-mark')] as HTMLElement[];
    expect(marks.map(mark => mark.getAttribute('data-tone'))).toEqual(['ok', 'err']);
    expect(marks.map(mark => mark.style.getPropertyValue('--mark-at'))).toEqual([
      '25.00%',
      '100.00%',
    ]);
  });

  test('the first rows are not new; a row that arrives later draws in; a repeat does not', async () => {
    const first = [row('a'), row('b')];
    const {container, root} = await mounted(first);
    expect(container.querySelectorAll('[data-entering]')).toHaveLength(0);
    await show(root, [...first, row('c')]);
    expect(options(container).map(one => one.hasAttribute('data-entering'))).toEqual([
      false,
      false,
      true,
    ]);
    await show(root, [...first, row('c', {figure: '$1'})]);
    expect(options(container).filter(one => one.hasAttribute('data-entering'))).toHaveLength(1);
  });
});
