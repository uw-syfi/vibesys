/**
 * The headers of a `ColumnList`: a sortable header keeps one slot for its arrow whether or not the
 * list is sorted by it, so sorting moves nothing; and the edge of every column but the one that fills
 * resizes it, by the arrow keys or a drag, within bounds.
 */
import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {tracksOf} from './components/column-tracks.js';
import {
  ColumnList,
  type ColumnWidths,
  clampColumnWidth,
  type ListColumn,
  MAX_COLUMN_WIDTH,
  MIN_COLUMN_WIDTH,
  widthToShowAll,
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

interface Item {
  readonly id: string;
}

type Direction = 'asc' | 'desc' | null;

async function mounted(options: {
  readonly direction: Direction;
  readonly widths?: ColumnWidths;
  readonly log: string[];
}): Promise<HTMLElement> {
  const columns: ListColumn<Item>[] = [
    {key: 'name', header: 'Name', size: 'fill', cell: item => item.id},
    {
      key: 'cost',
      header: 'Cost',
      size: 'short',
      sort: {direction: options.direction, press: () => options.log.push('sort cost')},
      cell: () => '1',
    },
    {key: 'best', header: 'Best', size: 'wide', cell: () => '2'},
  ];
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () =>
    createRoot(container).render(
      <ColumnList
        label="Items"
        columns={columns}
        rows={[{id: 'a'}]}
        rowKey={item => item.id}
        select={() => undefined}
        widths={options.widths ?? {}}
        resize={(key, width) => options.log.push(`resize ${key} ${width}`)}
      />,
    ),
  );
  return container;
}

const grips = (container: HTMLElement): HTMLElement[] => [
  ...container.querySelectorAll<HTMLElement>('hr.vs-columnlist-grip'),
];

describe('a sortable header', () => {
  test('draws the same boxes sorted either way or not, with the arrow in a slot it always keeps', async () => {
    const shapes: string[] = [];
    for (const direction of [null, 'asc', 'desc'] as const) {
      const container = await mounted({direction, log: []});
      const head = container.querySelector('[data-key="cost"]');
      expect(head?.querySelectorAll('.vs-columnlist-sortmark')).toHaveLength(1);
      const mark = head?.querySelector('.vs-columnlist-sortmark');
      expect(mark?.querySelector('svg') !== null).toBe(direction !== null);
      expect(mark?.textContent).toBe(direction === null ? '↕' : '');
      expect(head?.getAttribute('data-sorted')).toBe(direction);
      shapes.push(
        [...(head?.querySelectorAll('*') ?? [])]
          .filter(each => each.tagName !== 'svg' && each.tagName !== 'path')
          .map(each => each.className)
          .join('|'),
      );
    }
    expect(new Set(shapes).size).toBe(1);
  });

  test('is a button that sorts, and a header without a sort is plain text', async () => {
    const log: string[] = [];
    const container = await mounted({direction: null, log});
    const buttons = [...container.querySelectorAll<HTMLElement>('.vs-columnlist-head button')];
    expect(buttons.map(button => button.getAttribute('aria-label'))).toEqual(['Sort by Cost']);
    await act(async () => buttons[0]?.click());
    expect(log).toEqual(['sort cost']);
  });
});

describe('the edge of a column', () => {
  test('every column but the one that fills has one, named for the column', async () => {
    const container = await mounted({direction: null, log: []});
    expect(grips(container).map(each => each.getAttribute('aria-label'))).toEqual([
      'Resize Cost',
      'Resize Best',
    ]);
    expect(grips(container).map(each => each.tagName)).toEqual(['HR', 'HR']);
    expect(grips(container).map(each => each.getAttribute('title'))).toEqual([
      'Resize Cost column (drag or use arrow keys; double-click or press Enter to reset)',
      'Resize Best column (drag or use arrow keys; double-click or press Enter to reset)',
    ]);
    expect(
      grips(container).map(each => [
        each.getAttribute('aria-valuemin'),
        each.getAttribute('aria-valuemax'),
      ]),
    ).toEqual([
      [String(MIN_COLUMN_WIDTH), String(MAX_COLUMN_WIDTH)],
      [String(MIN_COLUMN_WIDTH), String(MAX_COLUMN_WIDTH)],
    ]);
    expect(grips(container).every(each => each.parentElement?.hasAttribute('data-resizable'))).toBe(
      true,
    );
    const css = readFileSync(new URL('columnlist.css', import.meta.url), 'utf8');
    const mark = css.slice(
      css.indexOf('.vs-columnlist-grip::after'),
      css.indexOf('}', css.indexOf('.vs-columnlist-grip::after')),
    );
    expect(mark).toContain('content: "";');
    expect(mark).toContain('width: var(--hairline);');
    expect(mark).not.toContain('↔');
    const focus = css.slice(
      css.indexOf('.vs-columnlist-grip:focus-visible {'),
      css.indexOf('}', css.indexOf('.vs-columnlist-grip:focus-visible {')),
    );
    expect(focus).toContain('outline: none;');
    expect(focus).not.toContain('border-radius:');
  });

  test('moves by the arrow keys within the bounds, and goes back to its own width on Enter or a double press', async () => {
    const log: string[] = [];
    const container = await mounted({direction: null, widths: {cost: 100}, log});
    const [edge] = grips(container);
    if (edge === undefined) throw new Error('no edge');
    const key = async (name: string): Promise<void> => {
      await act(async () => {
        edge.dispatchEvent(
          new KeyboardEvent('keydown', {key: name, bubbles: true, cancelable: true}),
        );
      });
    };
    await key('ArrowRight');
    await key('ArrowLeft');
    await key('Enter');
    await act(async () => {
      edge.dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
    });
    expect(log).toEqual([
      'resize cost 116',
      'resize cost 84',
      'resize cost null',
      'resize cost null',
    ]);
  });

  test('is dragged with the pointer: the column shows the width as it goes and gives it on release', async () => {
    const log: string[] = [];
    const container = await mounted({direction: null, widths: {cost: 100}, log});
    const [edge] = grips(container);
    if (edge === undefined) throw new Error('no edge');
    const at = (type: string, x: number): void => {
      edge.dispatchEvent(
        Object.assign(new MouseEvent(type, {bubbles: true, button: 0, clientX: x}), {
          pointerId: 1,
        }),
      );
    };
    await act(async () => at('pointerdown', 200));
    await act(async () => at('pointermove', 240));
    const during = container
      .querySelector<HTMLElement>('.vs-columnlist')
      ?.style.getPropertyValue('--columns');
    expect(during).toContain('140px');
    await act(async () => at('pointerup', 240));
    expect(log).toEqual(['resize cost 140']);
    expect(
      container.querySelector<HTMLElement>('.vs-columnlist')?.style.getPropertyValue('--columns'),
    ).toContain('100px');
  });
});

describe('the widths a person gave', () => {
  test('are always within bounds, whatever the edge is dragged to', () => {
    fc.assert(
      fc.property(fc.double({min: -1e6, max: 1e6, noNaN: true}), px => {
        const width = clampColumnWidth(px);
        expect(width).toBeGreaterThanOrEqual(MIN_COLUMN_WIDTH);
        expect(width).toBeLessThanOrEqual(MAX_COLUMN_WIDTH);
        expect(clampColumnWidth(width)).toBe(width);
      }),
    );
  });

  test('set a column in every tier, never the one that fills, and the width every column needs counts them', () => {
    const columns = [
      {key: 'name', size: 'fill'},
      {key: 'cost', size: 'short'},
      {key: 'best', size: 'wide', optional: true},
    ] as const;
    fc.assert(
      fc.property(fc.integer({min: 1, max: 900}), width => {
        const widths = {name: width, cost: width};
        for (const tier of ['wide', 'narrow', 'tight'] as const) {
          const tracks = tracksOf(columns, tier, widths);
          expect(tracks).toContain(`${clampColumnWidth(width)}px`);
          expect(tracks.startsWith(`${clampColumnWidth(width)}px`)).toBe(false);
        }
      }),
    );
    // A column dragged to its widest needs more room than its own width did.
    expect(widthToShowAll(columns, {cost: MAX_COLUMN_WIDTH})).toBeGreaterThan(
      widthToShowAll(columns),
    );
  });
});

describe('the cells of a list', () => {
  test('cut their text with an ellipsis inside the cell, never into the next column', () => {
    const css =
      ['columnlist.css'].map(name => readFileSync(new URL(name, import.meta.url), 'utf8'))[0] ?? '';
    const rule = (selector: string): string => {
      const at = css.indexOf(`\n${selector} {`);
      return css.slice(at, css.indexOf('}', at));
    };
    for (const selector of ['.vs-columnlist-cell', '.vs-columnlist-lines > *']) {
      expect(rule(selector)).toContain('overflow: hidden;');
      expect(rule(selector)).toContain('text-overflow: ellipsis;');
      expect(rule(selector)).toContain('white-space: nowrap;');
    }
  });

  test('keep a chip as wide as its words wherever it sits', () => {
    const inputs = readFileSync(new URL('inputs.css', import.meta.url), 'utf8');
    const at = inputs.indexOf('\n.vs-chip {');
    expect(inputs.slice(at, inputs.indexOf('}', at))).toContain('width: fit-content;');
  });
});
