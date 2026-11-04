/**
 * A row of a `ColumnList` is named by its first column, whatever the other cells say (a cell that
 * names another row, as a link to the row it builds on, does not rename this one), and a press picks
 * the row unless it lands on the word of a `CellLink` that takes it.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {CellLink, ColumnList, type ListColumn} from './index.js';

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
  readonly name: string;
  readonly parent: string;
}

async function mounted(
  items: readonly Item[],
  picked: string[],
  opened: string[],
): Promise<HTMLElement> {
  const columns: ListColumn<Item>[] = [
    {key: 'name', header: 'Name', size: 'fill', cell: item => item.name},
    {
      key: 'parent',
      header: 'Built on',
      size: 'short',
      act: item => {
        opened.push(item.id);
        return true;
      },
      cell: item => <CellLink label={item.parent} />,
    },
  ];
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () =>
    createRoot(container).render(
      <ColumnList
        label="Items"
        columns={columns}
        rows={items}
        rowKey={item => item.id}
        select={key => picked.push(key)}
        describe={item => `built on ${item.parent}`}
      />,
    ),
  );
  return container;
}

const WORDS = fc.stringMatching(/^[A-Za-z]{3,10}$/);
const ITEMS = fc
  .uniqueArray(fc.record({name: WORDS, parent: WORDS}), {
    minLength: 2,
    maxLength: 6,
    selector: item => item.name,
  })
  .map(list => list.map((item, at): Item => ({id: `i${at}`, ...item})));

describe('a column list row', () => {
  test('property: its name is its first column, its description the rest, whatever the other cells say', async () => {
    await fc.assert(
      fc.asyncProperty(ITEMS, async items => {
        const container = await mounted(items, [], []);
        const rows = [...container.querySelectorAll('[role="option"]')];
        expect(rows).toHaveLength(items.length);
        rows.forEach((row, at) => {
          const name = row.getAttribute('aria-labelledby');
          expect(name).not.toBeNull();
          expect(document.getElementById(name ?? '')?.textContent).toBe(items[at]?.name);
          expect(row.getAttribute('aria-description')).toBe(`built on ${items[at]?.parent}`);
        });
        container.remove();
      }),
      {numRuns: 25, seed: 20261031},
    );
  });

  test('property: a press picks the row, except on the word of a link that takes it', async () => {
    await fc.assert(
      fc.asyncProperty(ITEMS, fc.nat(), async (items, which) => {
        const picked: string[] = [];
        const opened: string[] = [];
        const container = await mounted(items, picked, opened);
        const at = which % items.length;
        const id = items[at]?.id ?? '';
        const row = container.querySelectorAll('[role="option"]')[at] as HTMLElement;
        const cell = row.querySelector('[data-column="parent"]') as HTMLElement;
        await act(async () => cell.click());
        expect({picked, opened}).toEqual({picked: [id], opened: []});
        await act(async () => (cell.querySelector('.vs-columnlist-link') as HTMLElement).click());
        expect({picked, opened}).toEqual({picked: [id], opened: [id]});
        container.remove();
      }),
      {numRuns: 25, seed: 20261031},
    );
  });
});
