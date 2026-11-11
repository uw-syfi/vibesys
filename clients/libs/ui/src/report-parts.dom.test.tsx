/**
 * The parts a dense report is built from, through the `.` entry only: a chart that holds its frame
 * with nothing in it, writes the value at a corner of its line and never draws over its axis; a table
 * that sorts by its headers; the labelled line; filter chips with counts; the overview strip.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {
  Brief,
  Card,
  Chart,
  Chip,
  CountedFact,
  chartPlotMargins,
  DotLegend,
  FactLine,
  MainSide,
  MutedFact,
  RangeStrip,
  ShareBar,
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

describe('Chart notes at the right edge', () => {
  test('a note that would pass the plot’s right edge is written to the left of its place', async () => {
    const root = await mount(
      <Chart
        label="c"
        points={[]}
        xDomain={[0, 100]}
        yDomain={[0, 10]}
        notes={[{x: 100, y: 5, label: '15'}]}
      />,
    );
    const note = root.querySelector('text.vs-chart-note');
    expect(note?.getAttribute('text-anchor')).toBe('end');
    const right = Number(root.querySelector('line.vs-chart-axis:last-of-type')?.getAttribute('x2'));
    expect(Number(note?.getAttribute('x'))).toBeLessThan(right);
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

describe('Table columns that fit their content', () => {
  const rows = [{name: 'a'}];

  test('a column that fits never wraps, and the lead column is the one that takes what is left', async () => {
    const root = await mount(
      <Table
        label="t"
        rows={rows}
        rowKey={row => row.name}
        columns={[
          {key: 'name', header: 'Name', lead: true, cell: row => row.name},
          {key: 'state', header: 'State', fit: true, cell: () => 'Live'},
        ]}
      />,
    );
    const [name, state] = [...root.querySelectorAll('tbody td')];
    expect(state?.getAttribute('data-fit')).toBe('true');
    expect(name?.getAttribute('data-fit')).toBeNull();
  });

  test('a reserve is drawn in the header for its width, hidden from assistive technology', async () => {
    const root = await mount(
      <Table
        label="t"
        rows={rows}
        rowKey={row => row.name}
        columns={[
          {key: 'name', header: 'Name', cell: row => row.name},
          {key: 'cpu', header: 'CPU', reserve: '100.0% of it', cell: () => null},
        ]}
      />,
    );
    const [name, cpu] = [...root.querySelectorAll('th')];
    expect(name?.querySelector('.vs-table-reserve')).toBeNull();
    const reserve = cpu?.querySelector('.vs-table-reserve');
    expect(reserve?.textContent).toBe('100.0% of it');
    expect(reserve?.getAttribute('aria-hidden')).toBe('true');
    // The header's own name is what a reader hears, and only that.
    expect(cpu?.textContent?.startsWith('CPU')).toBe(true);
  });
});

describe('Table cells that span columns, and rows that group', () => {
  interface Row {
    readonly name: string;
    readonly note: boolean;
  }
  const rows: Row[] = [
    {name: 'a', note: true},
    {name: 'b', note: false},
  ];
  const columns = [
    {key: 'name', header: 'Name', cell: (row: Row) => row.name},
    {
      key: 'x',
      header: 'X',
      span: (row: Row) => (row.note ? 5 : 1),
      cell: (row: Row) => (row.note ? 'note' : 'x'),
    },
    {key: 'y', header: 'Y', cell: () => 'y'},
    {key: 'z', header: 'Z', cell: () => 'z'},
  ];

  test('a cell that spans covers the cells after it, never more than are left, and others keep theirs', async () => {
    const root = await mount(
      <Table label="t" rows={rows} rowKey={row => row.name} columns={columns} />,
    );
    const [first, second] = [...root.querySelectorAll('tbody tr')];
    expect([...(first?.querySelectorAll('td') ?? [])].map(cell => cell.textContent)).toEqual([
      'a',
      'note',
    ]);
    expect(first?.querySelectorAll('td')[1]?.getAttribute('colspan')).toBe('3');
    expect([...(second?.querySelectorAll('td') ?? [])].map(cell => cell.textContent)).toEqual([
      'b',
      'x',
      'y',
      'z',
    ]);
    expect(second?.querySelector('td[colspan]')).toBeNull();
  });

  test('a grouped table says so, for the room above the row that opens a group', async () => {
    const root = await mount(
      <Table label="t" rows={rows} rowKey={row => row.name} grouped columns={columns} />,
    );
    expect(root.querySelector('.vs-table')?.getAttribute('data-grouped')).toBe('true');
  });
});

describe('the labelled line, the main and side columns, the filter chips', () => {
  test('a brief is one block: its label, its lead, and each field as a label over its text, with nothing bold', async () => {
    const full = await mount(
      <Brief
        label="Lead's summary · 2h ago"
        lead="Headline"
        leadTitle="Headline"
        fields={[
          {label: 'Changed', text: 'a', title: 'a'},
          {label: 'Decided', text: 'b', title: 'b'},
        ]}
        more={<button type="button">Read the whole report</button>}
      />,
    );
    const block = full.querySelector('section.vs-brief');
    expect(
      [...(block?.querySelectorAll('.vs-brief-field') ?? [])].map(each => each.textContent),
    ).toEqual(['Changeda', 'Decidedb']);
    expect(block?.querySelector('.vs-brief-clamp[data-kind="lead"]')?.textContent).toBe('Headline');
    // Nothing is cut (no layout here measures a cut), so there is no way to the whole text.
    expect(block?.querySelector('.vs-brief-more')).toBeNull();
    expect(block?.querySelector('b, strong, h1, h2, h3')).toBeNull();
  });

  test('a brief with nothing to say yet is its one quiet line', async () => {
    const none = await mount(<Brief empty="The lead has not reported yet" />);
    expect(none.textContent).toBe('The lead has not reported yet');
    expect(none.querySelector('section')).toBeNull();
  });

  test('a share bar draws a part in the color of its tone, the one its dot has', async () => {
    const root = await mount(
      <ShareBar
        label="x"
        bare
        parts={[
          {key: 'a', label: 'Kept', value: 2, figure: '2', series: 1, tone: 'ok'},
          {key: 'b', label: 'Open', value: 1, figure: '1', series: 2},
        ]}
      />,
    );
    const colors = [...root.querySelectorAll<HTMLElement>('.vs-share-segment')].map(each =>
      each.style.getPropertyValue('--seg-color'),
    );
    expect(colors).toEqual(['var(--ok)', 'var(--series-2)']);
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

describe('round three parts', () => {
  test('a mark at either end of the x axis stands inside the plot, so it is drawn whole', async () => {
    const root = await mount(
      <Chart
        label="c"
        points={[
          {x: 0, y: 1},
          {x: 100, y: 2},
        ]}
        xDomain={[0, 100]}
        yDomain={[0, 3]}
      />,
    );
    const [left, right] = [
      Number(root.querySelector('line.vs-chart-axis')?.getAttribute('x1')),
      Number(root.querySelector('line.vs-chart-axis:last-of-type')?.getAttribute('x2')),
    ];
    const dots = [...root.querySelectorAll('circle.vs-chart-point')].map(each =>
      Number(each.getAttribute('cx')),
    );
    expect(Math.min(...dots)).toBeGreaterThanOrEqual(left + 3.5);
    expect(Math.max(...dots)).toBeLessThanOrEqual(right - 3.5);
  });

  test('a marker is a rule with a word and a hint, and its word stays inside the plot', async () => {
    await fc.assert(
      fc.asyncProperty(fc.double({min: 0, max: 100, noNaN: true}), async x => {
        const root = await mount(
          <Chart
            label="c"
            points={[]}
            xDomain={[0, 100]}
            marker={{
              x,
              label: 'all measurements from here',
              hint: 'The earlier ones are summarised.',
            }}
          />,
        );
        expect(root.querySelector('.vs-chart-marker title')?.textContent).toMatch(/summarised/);
        const word = root.querySelector('.vs-chart-marker text');
        const at = Number(word?.getAttribute('x'));
        const right = Number(
          root.querySelector('line.vs-chart-axis:last-of-type')?.getAttribute('x2'),
        );
        const left = Number(root.querySelector('line.vs-chart-axis')?.getAttribute('x1'));
        expect(at).toBeGreaterThanOrEqual(left - 1);
        expect(at).toBeLessThanOrEqual(right + 1);
      }),
      {numRuns: 20, seed: 9},
    );
  });

  test('the margins a chart tells are where its plot starts', async () => {
    const root = await mount(
      <Chart label="c" points={[]} yTicks={[{value: 1, label: '1234'}]} yLabelChars={4} />,
    );
    expect(Number(root.querySelector('line.vs-chart-axis')?.getAttribute('x1'))).toBe(
      chartPlotMargins(4).left,
    );
  });

  test('a card puts what stands beside its heading right after it, and its action at the end', async () => {
    const root = await mount(
      <Card section="Performance" beside={<span>key</span>} action={<span>range</span>}>
        x
      </Card>,
    );
    const head = root.querySelector('.vs-card-head');
    expect([...(head?.children ?? [])].map(each => each.className)).toEqual([
      'vs-card-title',
      'vs-card-beside',
      'vs-card-action',
    ]);
  });

  test('a card keeps its section label whole when its key and action need the same narrow line', () => {
    const css = readFileSync(new URL('./surfaces.css', import.meta.url), 'utf8');
    const declarations = (selector: string): string => {
      const start = css.indexOf(`\n${selector} {`);
      expect(start).toBeGreaterThan(-1);
      return css.slice(start, css.indexOf('}', start));
    };
    expect(declarations('.vs-card-title')).toContain('flex: none;');
    expect(declarations('.vs-card-title')).toContain('white-space: nowrap;');
    expect(declarations('.vs-card-head')).toContain('flex-wrap: wrap;');
  });

  test('a table header writes the unit of its values small beneath it', async () => {
    const root = await mount(
      <Table
        label="t"
        rows={[{a: 1}]}
        rowKey={row => String(row.a)}
        columns={[{key: 'a', header: 'Throughput', unit: 'ops/s', cell: row => row.a}]}
      />,
    );
    expect(root.querySelector('th .vs-table-unit')?.textContent).toBe('ops/s');
  });

  test('a table header says what it means on the pointer, for a short header', async () => {
    const root = await mount(
      <Table
        label="t"
        rows={[{a: 1}]}
        rowKey={row => String(row.a)}
        columns={[{key: 'a', header: 'Input', hint: 'Input tokens', cell: row => row.a}]}
      />,
    );
    expect(root.querySelector('th')?.getAttribute('title')).toBe('Input tokens');
  });

  test('a sortable header sits on the line of the headers that have no arrow, not above them', () => {
    const css = readFileSync(new URL('./surfaces.css', import.meta.url), 'utf8');
    const start = css.indexOf('\n.vs-table-sort {');
    const rule = css.slice(start, css.indexOf('}', start));
    expect(start).toBeGreaterThan(-1);
    expect(rule).toContain('vertical-align: top');
  });

  test('the fifth tone is a chip and a dot of its own', async () => {
    const root = await mount(<Chip label="Parked" tone="alt" dot />);
    expect(root.querySelector('.vs-chip')?.getAttribute('data-tone')).toBe('alt');
    expect(root.querySelector('.vs-dot')?.getAttribute('data-tone')).toBe('alt');
  });
});

describe('figures in a line of facts', () => {
  test('are not set in tabular figures, which widen a dot and a dollar sign in the face and gap a figure', () => {
    const css = readFileSync(new URL('./frame.css', import.meta.url), 'utf8');
    const start = css.indexOf('\n.vs-fact {');
    const rule = css.slice(start, css.indexOf('}', start));
    expect(start).toBeGreaterThan(-1);
    expect(rule).not.toContain('tabular-nums');
  });

  test('a counted fact is one piece of text, so its figure and its words have a real space between them', async () => {
    const root = await mount(
      <FactLine spaced>
        <CountedFact figure="$0.10" words="spent" hint="At least this much." />
        <CountedFact lead="not read on" figure="2" words="machines" />
        <MutedFact text="no ideas yet" hint="Nothing was proposed." />
      </FactLine>,
    );
    const facts = [...root.querySelectorAll('.vs-fact')];
    expect(facts.map(each => each.textContent)).toEqual([
      '$0.10 spent',
      'not read on 2 machines',
      'no ideas yet',
    ]);
    expect(facts.map(each => each.getAttribute('title'))).toEqual([
      'At least this much.',
      null,
      'Nothing was proposed.',
    ]);
    expect(facts[0]?.querySelector('.vs-text .vs-text')?.textContent).toBe('$0.10');
  });
});
