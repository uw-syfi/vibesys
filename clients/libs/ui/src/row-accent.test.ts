/**
 * The accent of a selected row is a straight bar. It used to be the row's own left border, which on
 * a row with rounded corners bends into a curve at the top and bottom. Every list that draws one
 * (`ColumnList`, `PickList`, the Gantt tree) takes the bar from one rule and sets only `--row-accent`.
 */
import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';

const source = ['./frame.css', './columnlist.css']
  .map(name => readFileSync(new URL(name, import.meta.url), 'utf8'))
  .join('\n');

/** Every rule of the stylesheet as [selector list, declarations]. */
function rules(): readonly (readonly [string, string])[] {
  return [...source.matchAll(/(^|\n)([^\n{}/][^{}]*)\{([^}]*)\}/g)].map(
    found => [(found[2] ?? '').trim(), found[3] ?? ''] as const,
  );
}

const ROWS = ['.vs-columnlist-row', '.vs-pickrow', '.vs-gantt-row'] as const;

describe('the accent of a row', () => {
  test("no rule colors a row's left border: the border is only the bar's room", () => {
    for (const [selector, body] of rules()) {
      if (!ROWS.some(row => selector.includes(row))) continue;
      const colored = [...body.matchAll(/border-left-color:\s*([^;]+);/g)].some(
        found => found[1] !== 'transparent',
      );
      expect({selector, colored}).toEqual({selector, colored: false});
    }
  });

  test('every list of rows draws the accent as a bar that stops short of the rounded corners', () => {
    const bar = rules().find(([selector]) =>
      ROWS.every(row => selector.includes(`${row}::before`)),
    );
    if (bar === undefined) throw new Error('no shared rule draws the accent bar');
    const [, body] = bar;
    expect(body).toContain('position: absolute;');
    expect(body).toContain('top: var(--radius);');
    expect(body).toContain('bottom: var(--radius);');
    expect(body).toContain('background: var(--row-accent, transparent);');
    const anchored = rules().find(
      ([selector]) => ROWS.every(row => selector.includes(row)) && !selector.includes('::'),
    );
    expect(anchored?.[1]).toContain('position: relative;');
  });

  test('a selected row sets its accent, and the selection of each list does', () => {
    for (const [row, selected] of [
      ['.vs-columnlist-row', '.vs-columnlist-row[aria-selected="true"]'],
      ['.vs-pickrow', '.vs-pickrow[aria-selected="true"]'],
      ['.vs-gantt-row', '.vs-gantt-row[aria-selected="true"]'],
    ] as const) {
      const rule = rules().find(([selector]) => selector === selected);
      expect({row, accent: rule?.[1].includes('--row-accent:')}).toEqual({row, accent: true});
    }
  });
});
