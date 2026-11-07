/**
 * The report's cost card through the DOM: every table it draws has one figure under each header, and
 * the Cost header stands over dollars, never over a count of tokens.
 */
import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import type {CostFeedSnapshot, CostView} from '../../../state/index.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {costViewArb} from '../../../testing/orchestrator/cost-arbitrary.js';
import {PRICED_AND_UNPRICED} from '../../../testing/orchestrator/cost-views.js';
import {CostCard} from './cost-card.js';

installDom();

const live: CostFeedSnapshot = {status: {_tag: 'Live'}, freshness: 'live'};

/** Each table of the card as its headers and, for each row, its cells. */
function tablesOf(view: CostView): {headers: string[]; rows: string[][]}[] {
  const screen = mount(
    <CostCard cost={view} feed={live} retry={() => undefined} open={() => undefined} />,
  );
  const tables = [...screen.container.querySelectorAll('table')].map(table => ({
    headers: [...table.querySelectorAll('thead th')].map(cell => cell.textContent ?? ''),
    rows: [...table.querySelectorAll('tbody tr')].map(row =>
      [...row.children].map(cell => cell.textContent ?? ''),
    ),
  }));
  screen.unmount();
  return tables;
}

describe('the cost card', () => {
  it('puts the figures of a row under the headers of its table, for any cost the host serves', () => {
    fc.assert(
      fc.property(costViewArb, view => {
        for (const {headers, rows} of tablesOf(view)) {
          for (const cells of rows) expect(cells).toHaveLength(headers.length);
        }
      }),
      {numRuns: 25},
    );
  });

  it('shows dollars, or a dash for a price never reported, under Cost and counts under Tokens', () => {
    const [table] = tablesOf({...PRICED_AND_UNPRICED});
    if (table === undefined) throw new Error('the card has its table');
    const at = (header: string): number => table.headers.indexOf(header);
    expect(at('Cost')).toBeGreaterThan(-1);
    expect(at('Tokens')).toBeGreaterThan(-1);
    for (const cells of table.rows) {
      expect(cells[at('Cost')]).toMatch(/^(\$[\d,.]+|—)$/);
      expect(cells[at('Tokens')]).toMatch(/^[\d.,]+[kKMBT]?$/);
      expect(cells[at('Agents')]).toMatch(/^×\d+$/);
    }
  });
});
