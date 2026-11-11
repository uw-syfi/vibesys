/**
 * The report's agents-and-cost panel through the DOM: every table it draws has one figure under each
 * header, the Cost header stands over dollars and the Tokens header over counts, and Running says how
 * many of the type's agents are working, as the host counted them.
 */
import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import type {AgentsStrip, CostFeedSnapshot, CostView} from '../../../state/index.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {costViewArb} from '../../../testing/orchestrator/cost-arbitrary.js';
import {PRICED_AND_UNPRICED} from '../../../testing/orchestrator/cost-views.js';
import {AgentsAndCost} from './agents-cost.js';

installDom();

const live: CostFeedSnapshot = {status: {_tag: 'Live'}, freshness: 'live'};

/** A strip that counts every type of the view as having 4 working of the type's agents. */
const stripOf = (view: CostView): AgentsStrip => ({
  types: view.roles.map(role => ({role: role.role, total: role.agents, running: 1})),
  total: view.roles.reduce((sum, role) => sum + role.agents, 0),
  running: view.roles.length,
});

/** The table of the panel as its headers and, for each row, its cells. */
function tableOf(
  view: CostView | undefined,
  agents: AgentsStrip,
): {headers: string[]; rows: string[][]; text: string} {
  const screen = mount(
    <AgentsAndCost
      agents={agents}
      cost={view}
      feed={live}
      retry={() => undefined}
      openAgents={() => undefined}
      open={() => undefined}
    />,
  );
  const table = screen.container.querySelector('table');
  const read = {
    headers: [...(table?.querySelectorAll('thead th') ?? [])].map(cell => cell.textContent ?? ''),
    rows: [...(table?.querySelectorAll('tbody tr') ?? [])].map(row =>
      [...row.children].map(cell => cell.textContent ?? ''),
    ),
    text: screen.container.textContent ?? '',
  };
  screen.unmount();
  return read;
}

describe('the agents and cost panel', () => {
  it('puts the figures of a row under the headers of its table, for any cost the host serves', () => {
    fc.assert(
      fc.property(costViewArb, view => {
        const {headers, rows} = tableOf(view, stripOf(view));
        // A view with no type is one row that says so across the table.
        for (const cells of rows)
          expect(cells).toHaveLength(
            rows.length === 1 && view.roles.length === 0 ? 1 : headers.length,
          );
      }),
      {numRuns: 25},
    );
  });

  it('shows dollars, or a dash for a price never reported, under Cost, counts under Tokens, and who is working under Running', () => {
    const view = {...PRICED_AND_UNPRICED};
    const table = tableOf(view, stripOf(view));
    const at = (header: string): number => table.headers.indexOf(header);
    expect(table.headers).toEqual(['Type', 'Running', 'Turns', 'Tokens', 'Cost']);
    expect(table.rows.length).toBeGreaterThan(0);
    for (const cells of table.rows) {
      expect(cells[at('Cost')]).toMatch(/^(≥\s)?(\$[\d,.]+|—)$/);
      expect(cells[at('Tokens')]).toMatch(/^[\d.,]+[kKMBT]?$/);
      expect(cells[at('Running')]).toMatch(/^\d+ of \d+$/);
    }
  });

  it('lists the types the run has had even before the cost is read, with dashes for what it has not read', () => {
    const view = {...PRICED_AND_UNPRICED};
    const table = tableOf(undefined, stripOf(view));
    expect(table.rows).toHaveLength(view.roles.length);
    for (const cells of table.rows) expect(cells.slice(2)).toEqual(['—', '—', '—']);
    expect(table.text).toContain('Reading cost');
  });

  it('says one line when there is no agent, under the same headers', () => {
    const table = tableOf(undefined, {types: [], total: 0, running: 0});
    expect(table.headers).toEqual(['Type', 'Running', 'Turns', 'Tokens', 'Cost']);
    expect(table.rows).toEqual([['No agents yet']]);
  });
});
