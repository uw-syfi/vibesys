/** Resizable panes and the docked panel in a DOM, through `.` only: the separator's semantics, keys and drag. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type JSX, type ReactNode, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Dock, Pane, type PaneResize, Panes} from './index.js';
import {SIZE} from './tokens/index.js';

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

const MIN = SIZE['pane-min'];
const MAX = SIZE['pane-max'];
const STEP = 16;
const words = (width: number): string => `${width} pixels wide`;

async function mount(node: ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(node));
  return container;
}

function one(container: HTMLElement, selector: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(selector);
  if (element === null) throw new Error(`no ${selector}`);
  return element;
}

async function press(element: HTMLElement, key: string): Promise<void> {
  await act(async () => {
    element.focus();
    element.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true}));
    element.dispatchEvent(new KeyboardEvent('keyup', {key, bubbles: true, cancelable: true}));
  });
}

async function pointer(element: HTMLElement, type: string, clientX: number): Promise<void> {
  await act(async () => {
    element.dispatchEvent(
      new PointerEvent(type, {clientX, button: 0, pointerId: 1, bubbles: true, cancelable: true}),
    );
  });
}

/** Two panes, the first or the last resizable, holding the width it is given as a page would. */
function Resizable(props: {
  readonly start: number | null;
  readonly edge: 'start' | 'end';
  readonly seen: number[];
}): JSX.Element {
  const [width, setWidth] = useState(props.start);
  const resize: PaneResize = {
    width,
    changeWidth: next => {
      props.seen.push(next);
      setWidth(next);
    },
    label: 'Resize the list',
    widthWords: words,
    edge: props.edge,
  };
  const other = (
    <Pane kicker="idea" title="Opened" width="fill">
      body
    </Pane>
  );
  const sized = (
    <Pane kicker="ideas" title="7" width={props.edge === 'end' ? 'master' : 'list'} resize={resize}>
      rows
    </Pane>
  );
  return (
    <Panes>
      {props.edge === 'end' ? sized : other}
      {props.edge === 'end' ? other : sized}
    </Panes>
  );
}

describe('a resizable pane', () => {
  test('has a focusable separator at its edge, named, controlling it, its value its width', async () => {
    for (const edge of ['end', 'start'] as const) {
      const container = await mount(<Resizable start={500} edge={edge} seen={[]} />);
      const separator = one(container, 'hr');
      const sized = one(container, 'section[data-sized]');
      expect(separator.getAttribute('aria-label')).toBe('Resize the list');
      expect(separator.getAttribute('aria-orientation')).toBe('vertical');
      expect(separator.getAttribute('aria-valuemin')).toBe(String(MIN));
      expect(separator.getAttribute('aria-valuemax')).toBe(String(MAX));
      expect(separator.getAttribute('aria-valuenow')).toBe('500');
      expect(separator.getAttribute('aria-valuetext')).toBe('500 pixels wide');
      expect(separator.tabIndex).toBe(0);
      expect(separator.getAttribute('aria-controls')).toBe(sized.id);
      expect(sized.style.getPropertyValue('--pane-size')).toBe('500px');
      // It sits between the two panes, at the resized pane's edge.
      const order = [...one(container, '.vs-panes').children].map(child => child.tagName);
      expect(order).toEqual(['SECTION', 'HR', 'SECTION']);
      expect(edge === 'end' ? separator.previousElementSibling : separator.nextElementSibling).toBe(
        sized,
      );
    }
  });

  test('keeps its default width until given one, then is drawn at the width given', async () => {
    const unsized = await mount(<Resizable start={null} edge="end" seen={[]} />);
    expect(unsized.querySelector('section[data-sized]')).toBeNull();
    expect(unsized.querySelector('section')?.style.getPropertyValue('--pane-size')).toBe('');
    const sized = await mount(<Resizable start={720} edge="end" seen={[]} />);
    expect(one(sized, 'section[data-sized]').style.getPropertyValue('--pane-size')).toBe('720px');
  });

  test('a key moves the pane its way or not at all, within bounds that hold the width drawn', async () => {
    // The pane may be drawn outside the bounds (a default share of a wide row, a narrow row): the
    // bounds then reach to it, so a step never goes against its key and the value stays true.
    const stepped = (width: number, by: number): number =>
      Math.max(Math.min(MIN, width), Math.min(Math.max(MAX, width), width + by));
    const starts = [MIN - 60, MIN, MIN + 7, 400, 640, MAX - 5, MAX, MAX + 133];
    for (const edge of ['end', 'start'] as const) {
      // Toward the pane narrows it: Right widens a pane on the left, Left one on the right.
      const grows = edge === 'end' ? 'ArrowRight' : 'ArrowLeft';
      const shrinks = edge === 'end' ? 'ArrowLeft' : 'ArrowRight';
      for (const start of starts) {
        const seen: number[] = [];
        const container = await mount(<Resizable start={start} edge={edge} seen={seen} />);
        const separator = one(container, 'hr');
        expect(separator.getAttribute('aria-valuemin')).toBe(String(Math.min(MIN, start)));
        expect(separator.getAttribute('aria-valuemax')).toBe(String(Math.max(MAX, start)));
        await press(separator, grows);
        await press(separator, shrinks);
        await press(separator, shrinks);
        await press(separator, 'Tab');
        const up = stepped(start, STEP);
        const down = stepped(up, -STEP);
        expect(seen).toEqual([up, down, stepped(down, -STEP)]);
        expect(up).toBeGreaterThanOrEqual(start);
        expect(down).toBeLessThanOrEqual(up);
        await press(separator, 'Home');
        await press(separator, 'End');
        expect(seen.slice(3)).toEqual([Math.min(MIN, seen[2] ?? 0), MAX]);
        expect(separator.getAttribute('aria-valuenow')).toBe(String(MAX));
        expect(separator.getAttribute('aria-valuetext')).toBe(`${MAX} pixels wide`);
      }
    }
  });

  test('a drag moves the pane as it goes and gives its width once let go', async () => {
    for (const edge of ['end', 'start'] as const) {
      const seen: number[] = [];
      const container = await mount(<Resizable start={500} edge={edge} seen={seen} />);
      const separator = one(container, 'hr');
      const sized = one(container, 'section[data-sized]');
      const way = edge === 'end' ? 1 : -1;
      await pointer(separator, 'pointermove', 900);
      await pointer(separator, 'pointerdown', 600);
      await pointer(separator, 'pointermove', 600 + 40 * way);
      expect(sized.style.getPropertyValue('--pane-size')).toBe('540px');
      await pointer(separator, 'pointermove', 600 + 2000 * way);
      expect(sized.style.getPropertyValue('--pane-size')).toBe(`${MAX}px`);
      await pointer(separator, 'pointermove', 600 - 2000 * way);
      expect(seen).toEqual([]);
      await pointer(separator, 'pointerup', 600);
      await pointer(separator, 'pointermove', 700);
      expect(seen).toEqual([MIN]);
      expect(sized.style.getPropertyValue('--pane-size')).toBe(`${MIN}px`);
    }
  });

  test('a cancelled drag leaves the pane as it was', async () => {
    const seen: number[] = [];
    const container = await mount(<Resizable start={500} edge="end" seen={seen} />);
    const separator = one(container, 'hr');
    await pointer(separator, 'pointerdown', 600);
    await pointer(separator, 'pointermove', 680);
    await pointer(separator, 'pointercancel', 680);
    await pointer(separator, 'pointerup', 680);
    expect(seen).toEqual([]);
    expect(one(container, 'section[data-sized]').style.getPropertyValue('--pane-size')).toBe(
      '500px',
    );
  });

  test('without a resize there is no separator', async () => {
    const container = await mount(
      <Panes>
        <Pane kicker="ideas" title="7" width="master">
          rows
        </Pane>
      </Panes>,
    );
    expect(container.querySelector('hr')).toBeNull();
  });
});

describe('a docked panel', () => {
  function Docked(props: {readonly open: boolean; readonly seen: number[]}): JSX.Element {
    const [width, setWidth] = useState<number | null>(400);
    return (
      <Dock
        label="Chat"
        side={props.open ? 'panel' : null}
        resize={{
          width,
          changeWidth: next => {
            props.seen.push(next);
            setWidth(next);
          },
          label: 'Resize the chat',
          widthWords: words,
        }}
      >
        screen
      </Dock>
    );
  }

  test('resizes from its left edge: dragging or pressing Left widens it', async () => {
    const seen: number[] = [];
    const container = await mount(<Docked open seen={seen} />);
    const separator = one(container, 'hr');
    expect(separator.getAttribute('aria-label')).toBe('Resize the chat');
    expect(separator.nextElementSibling).toBe(one(container, 'section[aria-label="Chat"]'));
    await press(separator, 'ArrowLeft');
    await pointer(separator, 'pointerdown', 500);
    await pointer(separator, 'pointermove', 470);
    await pointer(separator, 'pointerup', 470);
    expect(seen).toEqual([416, 446]);
  });

  test('has no separator while the panel is closed', async () => {
    const container = await mount(<Docked open={false} seen={[]} />);
    expect(container.querySelector('hr')).toBeNull();
  });
});
