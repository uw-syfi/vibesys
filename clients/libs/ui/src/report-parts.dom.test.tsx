/**
 * The parts a dense report is built from, through the `.` entry only: a chart that holds its frame
 * with nothing in it, writes the value at a corner of its line and never draws over its axis; a table
 * that sorts by its headers; the labelled line; filter chips with counts; the overview strip.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {
  Chart,
  DotLegend,
  LabelledLine,
  MainSide,
  RangeStrip,
  Table,
  windowMoved,
  windowOfDrag,
} from './index.js';

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

async function mount(node: ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () => createRoot(container).render(node));
  return container;
}

/** A moment written as fine as the step of its axis: milliseconds, seconds, minutes, then days. */
function byStep(at: number, _span: number, step: number): string {
  const digits = [
    [1000, 23],
    [60_000, 19],
    [86_400_000, 16],
  ] as const;
  const length = digits.find(([limit]) => step < limit)?.[1] ?? 10;
  return new Date(at).toISOString().slice(0, length);
}

const heightOf = (root: HTMLElement): string | null | undefined =>
  root.querySelector('svg.vs-chart')?.getAttribute('height');

describe('Chart with nothing to draw', () => {
  const zone = () => 0;
  const label = (at: number): string => new Date(at).toISOString().slice(11, 19);

  test('keeps its frame, its time axis and its height, and says why in the middle of the plot', async () => {
    const from = Date.UTC(2026, 10, 6, 22, 56, 12);
    const empty = await mount(
      <Chart
        label="c"
        size="tall"
        points={[]}
        xDomain={[from, from + 40_000]}
        timeAxis={{label, zone}}
        empty="Waiting for the input's measurement"
      />,
    );
    const full = await mount(
      <Chart
        label="c"
        size="tall"
        points={[{x: from + 8_000, y: 1}]}
        xDomain={[from, from + 40_000]}
        timeAxis={{label, zone}}
      />,
    );
    expect(empty.querySelector('.vs-chart-state')?.textContent).toBe(
      "Waiting for the input's measurement",
    );
    expect(empty.querySelector('.vs-chart-frame')).not.toBeNull();
    expect(heightOf(empty)).toBe(heightOf(full));
    expect(heightOf(empty)).toBe('200');
    const ticks = (root: HTMLElement) =>
      [...root.querySelectorAll('text.vs-chart-label')].map(text => text.textContent);
    expect(ticks(empty).length).toBeGreaterThan(1);
    expect(ticks(empty)).toEqual(ticks(full));
  });

  test('property: its time axis never repeats a label, from seconds to a week, at any width', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.integer({min: 1_000, max: 7 * 86_400_000}),
        fc.integer({min: 280, max: 1400}),
        async (length, width) => {
          const from = Date.UTC(2026, 10, 6, 22, 56, 12);
          const root = document.createElement('div');
          root.style.width = `${width}px`;
          document.body.append(root);
          await act(async () =>
            createRoot(root).render(
              <Chart
                label="c"
                points={[]}
                xDomain={[from, from + length]}
                timeAxis={{label: byStep, zone}}
                empty="x"
              />,
            ),
          );
          const labels = [...root.querySelectorAll('text.vs-chart-label')].map(t => t.textContent);
          expect(new Set(labels).size).toBe(labels.length);
        },
      ),
      {numRuns: 40, seed: 11},
    );
  });
});

describe('Chart notes', () => {
  const points = [
    {x: 1, y: 1},
    {x: 2, y: 2},
  ];

  test('writes each value beside its corner, and drops one that would cover a later one', async () => {
    const root = await mount(
      <Chart
        label="c"
        points={points}
        xDomain={[0, 100]}
        yDomain={[0, 3]}
        notes={[
          {x: 50, y: 1, label: '11.1k'},
          {x: 50.2, y: 1.01, label: '11.2k'},
          {x: 80, y: 2, label: '12.0k'},
        ]}
      />,
    );
    const words = [...root.querySelectorAll('text.vs-chart-note')].map(note => note.textContent);
    expect(words).toContain('12.0k');
    expect(words).toContain('11.2k');
    expect(words).not.toContain('11.1k');
  });

  test('property: no two notes overlap, whatever places they name', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(
          fc.record({
            x: fc.double({min: 0, max: 100, noNaN: true}),
            y: fc.double({min: 0, max: 100, noNaN: true}),
          }),
          {minLength: 1, maxLength: 20},
        ),
        async places => {
          const root = await mount(
            <Chart
              label="c"
              points={[]}
              xDomain={[0, 100]}
              yDomain={[0, 100]}
              notes={places.map((place, at) => ({...place, label: `${at + 100}`}))}
            />,
          );
          const boxes = [...root.querySelectorAll('text.vs-chart-note')].map(note => {
            const x = Number(note.getAttribute('x'));
            const width = (note.textContent ?? '').length * 7.2;
            const left = note.getAttribute('text-anchor') === 'end' ? x - width : x;
            return {left, right: left + width, y: Number(note.getAttribute('y'))};
          });
          for (const [i, a] of boxes.entries()) {
            for (const b of boxes.slice(i + 1)) {
              const apart = a.right <= b.left || b.right <= a.left || Math.abs(a.y - b.y) >= 14;
              expect(apart).toBe(true);
            }
          }
        },
      ),
      {numRuns: 30, seed: 5},
    );
  });
});

describe('Chart clipping', () => {
  test('draws its line and dots inside a clip of the plot, so no mark reaches an axis label', async () => {
    const root = await mount(
      <Chart
        label="c"
        points={[{x: -50, y: 5}]}
        steps={[
          {x: -50, y: 5},
          {x: 90, y: 5},
        ]}
        xDomain={[0, 100]}
        yDomain={[0, 10]}
        yTicks={[{value: 5, label: '5'}]}
      />,
    );
    const group = root.querySelector('g[clip-path]');
    expect(group?.querySelector('path.vs-chart-step')).not.toBeNull();
    expect(group?.querySelector('circle.vs-chart-point')).not.toBeNull();
    const clip = root.querySelector('clipPath rect');
    const axis = Number(root.querySelector('line.vs-chart-axis')?.getAttribute('x1'));
    expect(Number(clip?.getAttribute('x'))).toBeGreaterThanOrEqual(axis - 8);
    const label = root.querySelector('text.vs-chart-label');
    expect(Number(label?.getAttribute('x'))).toBeLessThan(Number(clip?.getAttribute('x')));
  });
});

describe('Table sorting', () => {
  interface Row {
    readonly name: string;
  }
  const rows: Row[] = [{name: 'b'}, {name: 'a'}];

  test('a sortable header is a button that asks for its column, and the sorted one says its order', async () => {
    const asked: string[] = [];
    const root = await mount(
      <Table
        label="t"
        rows={rows}
        rowKey={row => row.name}
        sort={{key: 'name', direction: 'ascending', change: key => asked.push(key)}}
        columns={[
          {key: 'name', header: 'Name', sortable: true, cell: row => row.name},
          {key: 'other', header: 'Other', cell: () => 'x'},
        ]}
      />,
    );
    const [first, second] = [...root.querySelectorAll('th')];
    expect(first?.getAttribute('aria-sort')).toBe('ascending');
    expect(second?.getAttribute('aria-sort')).toBeNull();
    expect(second?.querySelector('button')).toBeNull();
    await act(async () => first?.querySelector('button')?.click());
    expect(asked).toEqual(['name']);
  });

  test('a table that scrolls down is a feed of its own height, and dense rows say so', async () => {
    const root = await mount(
      <Table
        label="t"
        rows={rows}
        rowKey={row => row.name}
        scroll="down"
        dense
        columns={[{key: 'name', header: 'Name', cell: row => row.name}]}
      />,
    );
    expect(root.querySelector('.vs-table-scroll')?.getAttribute('data-scroll')).toBe('feed');
    expect(root.querySelector('.vs-table')?.getAttribute('data-dense')).toBe('true');
  });
});

describe('the labelled line, the main and side columns, the filter chips', () => {
  test('a line with no fields is only its label and lead, and one with fields lists each under its label', async () => {
    const bare = await mount(<LabelledLine label="Lead" lead="No report yet" />);
    expect(bare.querySelector('dl')).toBeNull();
    const full = await mount(
      <LabelledLine
        label="Lead"
        note="2h ago"
        lead="Headline"
        fields={[
          {label: 'Changed', text: 'a'},
          {label: 'Decided', text: 'b'},
        ]}
      />,
    );
    expect([...full.querySelectorAll('dt')].map(each => each.textContent)).toEqual([
      'Changed',
      'Decided',
    ]);
  });

  test('main and side keep the order of reading: the main column first', async () => {
    const root = await mount(<MainSide label="Report" main="M" side="S" />);
    expect(root.querySelector('.vs-mainside-grid')?.textContent).toBe('MS');
  });

  test('a filter chip names its count beside its label and the pressed one is marked', async () => {
    const pressed: boolean[] = [];
    const root = await mount(
      <DotLegend
        items={[
          {tone: 'ok', label: 'Kept', count: 12, on: true, toggle: on => pressed.push(on)},
          {tone: 'warn', label: 'Open', count: 0, disabled: true, toggle: () => undefined},
        ]}
      />,
    );
    const [kept, open] = [...root.querySelectorAll('button')];
    expect(kept?.textContent).toContain('Kept');
    expect(kept?.textContent).toContain('12');
    expect(kept?.getAttribute('aria-pressed')).toBe('true');
    expect(open?.hasAttribute('disabled')).toBe(true);
    await act(async () => kept?.click());
    expect(pressed).toEqual([false]);
  });
});

describe('the overview strip', () => {
  const domain = [0, 1000] as const;

  test('property: a dragged window is inside the domain, ordered, and the same whichever way it is dragged', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 100, max: 3000}),
        fc.integer({min: -200, max: 3200}),
        fc.integer({min: -200, max: 3200}),
        (width, a, b) => {
          const forward = windowOfDrag(domain, width, {from: a, to: b});
          const backward = windowOfDrag(domain, width, {from: b, to: a});
          expect(forward).toEqual(backward);
          if (forward === null) return;
          expect(forward[0]).toBeGreaterThanOrEqual(domain[0]);
          expect(forward[1]).toBeLessThanOrEqual(domain[1]);
          expect(forward[0]).toBeLessThanOrEqual(forward[1]);
        },
      ),
    );
  });

  test('a press that does not move chooses no window', () => {
    expect(windowOfDrag(domain, 500, {from: 100, to: 101})).toBeNull();
  });

  test('property: a window moved by the arrow keys keeps its length and stays inside the domain', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 0, max: 900}),
        fc.integer({min: 1, max: 100}),
        fc.double({min: -3, max: 3, noNaN: true}),
        (start, length, by) => {
          const moved = windowMoved(domain, [start, start + length], by);
          expect(moved[1] - moved[0]).toBeCloseTo(length, 6);
          expect(moved[0]).toBeGreaterThanOrEqual(domain[0] - 1e-9);
          expect(moved[1]).toBeLessThanOrEqual(domain[1] + 1e-9);
        },
      ),
    );
  });

  test('the arrow keys move the window, and Escape asks for the whole axis', async () => {
    const asked: ({readonly 0: number; readonly 1: number} | null)[] = [];
    const root = await mount(
      <RangeStrip
        label="Whole span"
        domain={domain}
        window={[100, 200]}
        valueText="Part of the run"
        steps={[
          {x: 0, y: 1},
          {x: 500, y: 2},
        ]}
        change={window => asked.push(window)}
      />,
    );
    const plot = root.querySelector('svg');
    const key = async (name: string): Promise<void> => {
      await act(async () => {
        plot?.dispatchEvent(
          new window.KeyboardEvent('keydown', {key: name, bubbles: true}) as never,
        );
      });
    };
    await key('ArrowRight');
    await key('Escape');
    expect(asked).toEqual([[110, 210], null]);
  });
});
