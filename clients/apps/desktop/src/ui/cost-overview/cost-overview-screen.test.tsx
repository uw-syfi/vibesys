import {describe, expect, it} from '@effect/vitest';
import type {AgentRole} from '@vibesys/orchestrator-proto';
import type {JSX} from 'react';
import type {HostCostView, HostStatus} from '../../state/index.js';
import {installDom, mount, type Screen} from '../../testing/dom/harness.js';
import {COST_CASES, type CostCase, costCaseView} from '../../testing/orchestrator/index.js';
import {COST_OVERVIEW_WORDS} from '../test-display-clock.js';
import {CostOverviewScreen} from './cost-overview-screen.js';

installDom();

const LIVE: HostStatus = {kind: 'live', cause: null, staleForMs: null};
const views = Object.fromEntries(COST_CASES.map(name => [name, costCaseView(name)])) as Record<
  CostCase,
  HostCostView
>;

function page(
  view: HostCostView | undefined,
  opened: string[] = [],
  status: HostStatus = LIVE,
  working: ReadonlyMap<AgentRole, number> = new Map(),
): JSX.Element {
  return (
    <CostOverviewScreen
      hosts={[
        {
          key: 'host-a',
          label: 'Host A',
          status,
          feed: {_tag: 'Live'},
          view,
          titles: {},
          started: {},
          working,
        },
      ]}
      words={COST_OVERVIEW_WORDS}
      open={(host, runId) => opened.push(`${host}/${runId}`)}
      retry={() => undefined}
    />
  );
}

const PANELS = ['Spend over time', 'By run', 'By agent type', 'By model'];
const headings = (screen: Screen): string[] =>
  [...screen.container.querySelectorAll('h2')].map(each => each.textContent ?? '');
const bars = (screen: Screen): number => screen.container.querySelectorAll('.vs-bars-bar').length;
/** The segment of the range control a person presses. */
const range = (screen: Screen, value: string): HTMLElement => {
  const found = screen.container.querySelector<HTMLElement>(
    `input[type="radio"][value="${value}"]`,
  );
  if (found === null) throw new Error(`no range ${value}`);
  return found;
};
const firstRun = (screen: Screen): string =>
  screen.container.querySelector('tr[data-selectable] td')?.textContent ?? '';

describe('the Cost page of one host', () => {
  it('has the same panels in the same order for every state: nothing read yet, no spend, one run, a week', () => {
    for (const view of [undefined, views.none, views.short, views.week]) {
      const screen = mount(page(view));
      expect(headings(screen)).toEqual(PANELS);
      expect(screen.container.querySelectorAll('.vs-mainside')).toHaveLength(1);
      screen.unmount();
    }
  });

  it('says in one line what each panel waits for, and keeps its table headers, when there is no spend', () => {
    const screen = mount(page(views.none));
    const text = screen.text();
    expect(text).toContain('No turns yet');
    expect(text).toContain('nothing spent yet');
    // The run's row is there with nothing made up for it: no zero cost, no zero tokens.
    expect(
      screen.container.querySelector('table[aria-label="Cost by run on Host A"]'),
    ).not.toBeNull();
    expect(text).not.toContain('$0');
    expect(screen.container.querySelectorAll('.vs-bars')).toHaveLength(0);
    screen.unmount();
    const reading = mount(page(undefined, [], {kind: 'waiting', cause: null, staleForMs: null}));
    expect(reading.text()).toContain('Reading cost');
    reading.unmount();

    const run = views.none.runs[0];
    if (run === undefined) throw new Error('the no-spend case has its live run');
    const unpriced = mount(
      page({
        ...views.none,
        turns: 1,
        unpricedTurns: 1,
        runs: [{...run, turns: 1, unpricedTurns: 1}],
      }),
    );
    expect(unpriced.text()).toContain('spend unknown');
    expect(unpriced.text()).not.toContain('nothing spent yet');
    unpriced.unmount();
  });

  it('puts the status and the facts on one line under the title, with no card of figures', () => {
    const screen = mount(page(views.short));
    const meta = screen.container.querySelector('.vs-page-meta');
    expect(meta?.querySelectorAll('.vs-factline')).toHaveLength(1);
    expect(meta?.querySelector('.vs-factline .vs-chip')?.textContent).toBe('Live');
    const text = meta?.textContent ?? '';
    expect(text).toContain('≥\u00a0$2.35 spent');
    expect(text).toContain('≥\u00a0$2.35 in the last hour');
    expect(text).toContain('1 run');
    expect(text).toContain('9 turns');
    expect(text).toContain('4 turns unpriced');
    const unpriced = [...(meta?.querySelectorAll('.vs-fact') ?? [])].find(fact =>
      fact.textContent?.includes('unpriced'),
    );
    expect(unpriced?.getAttribute('title')).toMatch(/tokens and no price/);
    expect(screen.text()).not.toContain('Last hour');
    screen.unmount();
  });

  it('draws its agents by type in the columns the run report uses, with how many are working of how many there have been', () => {
    const view = views.short;
    const [first] = view.roles;
    if (first === undefined) throw new Error('the case has a type');
    const screen = mount(page(view, [], LIVE, new Map([[first.role, 1]])));
    const table = screen.container.querySelector(
      'table[aria-label="Cost by agent type on Host A"]',
    );
    expect([...(table?.querySelectorAll('thead th') ?? [])].map(cell => cell.textContent)).toEqual([
      'Type',
      'Running',
      'Turns',
      'Input',
      'Output',
      'Cost',
    ]);
    const rows = [...(table?.querySelectorAll('tbody tr') ?? [])].map(row =>
      [...row.children].map(cell => cell.textContent),
    );
    expect(rows.map(cells => cells[1])).toEqual(
      view.roles.map((role, at) => `${at === 0 ? 1 : 0} of ${role.agents}`),
    );
    screen.unmount();
  });

  it('draws a short run as the few bars it has, and offers no range', () => {
    const screen = mount(page(views.short));
    expect(bars(screen)).toBeGreaterThan(0);
    expect(bars(screen)).toBeLessThan(30);
    expect(screen.container.querySelector('[role="radiogroup"]')).toBeNull();
    screen.unmount();
  });

  it('offers a range for a week, and the last day draws fewer bars than the whole week', () => {
    const screen = mount(page(views.week));
    const all = bars(screen);
    expect(screen.container.querySelector('[role="radiogroup"]')?.getAttribute('aria-label')).toBe(
      'Range of the spend chart',
    );
    screen.press(range(screen, '1d'));
    expect(bars(screen)).toBeLessThan(all);
    screen.press(range(screen, 'all'));
    expect(bars(screen)).toBe(all);
    screen.unmount();
  });

  it('writes each figure of the facts line one space from its words, never against them', () => {
    for (const view of [views.none, views.short, views.week]) {
      const screen = mount(page(view));
      const facts = [...screen.container.querySelectorAll('.vs-page-meta .vs-fact')];
      expect(facts.length).toBeGreaterThanOrEqual(3);
      for (const fact of facts) {
        const text = fact.textContent ?? '';
        // A figure is followed by a space (or ends the fact), and no word is joined to a figure.
        expect(text, text).not.toMatch(/[\d%]\p{L}/u);
        expect(text, text).toMatch(/^[^ ]+( [^ ]+)*$/);
      }
      expect(screen.container.querySelector('.vs-factline')?.getAttribute('data-spaced')).toBe(
        'true',
      );
      screen.unmount();
    }
  });

  it('draws a model by its id in the code face, with its color beside it in the table, and no made-up name', () => {
    const screen = mount(page(views.week));
    const table = screen.container.querySelector('table[aria-label="Cost by model on Host A"]');
    const names = [...(table?.querySelectorAll('tbody td:first-child [data-mono-id]') ?? [])].map(
      each => each.textContent,
    );
    expect(names).toEqual(expect.arrayContaining(['claude-opus-5-5', 'gpt-5-codex']));
    expect(table?.querySelectorAll('tbody .vs-series-swatch').length).toBe(
      table?.querySelectorAll('tbody tr').length,
    );
    expect(screen.text()).not.toContain('Claude Opus');
    screen.unmount();
  });

  it('says when the bounded model list or one of its names was shortened', () => {
    const screen = mount(page({...views.short, modelsTruncated: true}));
    expect(screen.text()).toContain(
      'The host shortened the model list or a model name. Totals still include every model.',
    );
    screen.unmount();
  });

  it('draws a run that finished as a neutral chip, and keeps tone for what wants a look', () => {
    const screen = mount(page(views.week));
    const chips = [...screen.container.querySelectorAll('tbody .vs-chip')];
    const tones = new Map(chips.map(chip => [chip.textContent, chip.getAttribute('data-tone')]));
    expect(tones.get('Finished')).toBe('neutral');
    expect(tones.get('Running')).toBe('live');
    expect(tones.get('Failed')).toBe('err');
    screen.unmount();
  });

  it('flags the budget of a run that is over it, with how far over on hover', () => {
    const screen = mount(page(views.week));
    const over = [...screen.container.querySelectorAll('tbody [data-tone="err"][title]')].filter(
      each => each.getAttribute('title')?.startsWith('Over budget by'),
    );
    expect(over.length).toBeGreaterThan(0);
    screen.unmount();
  });

  it('names the unit of every column of tokens, and the basis of the percentages', () => {
    const screen = mount(page(views.week));
    const headers = [
      ...screen.container.querySelectorAll('table[aria-label="Cost by run on Host A"] th'),
    ].map(each => each.textContent);
    expect(headers).toEqual(expect.arrayContaining(['Input', '% cached', 'Output', '% of total']));
    // The unit of a column of tokens is in its header's hint, which names tokens.
    const hints = new Map(
      [...screen.container.querySelectorAll('table[aria-label="Cost by run on Host A"] th')].map(
        th => [th.textContent, th.getAttribute('title')],
      ),
    );
    expect(hints.get('Input')).toMatch(/tokens/);
    expect(hints.get('Output')).toMatch(/tokens/);
    expect(hints.get('% cached')).toMatch(/input tokens/);
    expect(hints.get('% of total')).toMatch(/cost of every run/);
    screen.unmount();
  });

  it('keeps cost share and budget in the compact table before secondary metadata', () => {
    const screen = mount(page(views.week));
    const table = screen.container.querySelector('table[aria-label="Cost by run on Host A"]');
    const columns = new Map(
      [...(table?.querySelectorAll('thead th') ?? [])].map(header => [
        header.textContent,
        header.hasAttribute('data-wide'),
      ]),
    );
    expect(columns.get('% of total')).toBe(false);
    expect(columns.get('Budget')).toBe(false);
    expect(columns.get('Started')).toBe(true);
    expect(columns.get('Input')).toBe(true);
    expect(columns.get('% cached')).toBe(true);
    expect(columns.get('Output')).toBe(true);
    screen.unmount();
  });

  it('says how long each bar lasts, and writes the axis with a zero and thousands separated', () => {
    const screen = mount(page(views.week));
    expect(screen.text()).toMatch(/per (\d+ )?(minute|hour|day)s?/);
    const axis = [...screen.container.querySelectorAll('.vs-bars-scale .vs-bars-tick')].map(
      each => each.textContent,
    );
    expect(axis).toContain('$0');
    expect(axis.join(' ')).toMatch(/\$\d{1,3},\d{3}/);
    screen.unmount();
  });

  it('stands each bar at its time with the width of its bucket, on an axis of days for a week', () => {
    const screen = mount(page(views.week));
    const bars = [...screen.container.querySelectorAll<HTMLElement>('.vs-bars-bar')];
    const lefts = bars.map(bar => Number.parseFloat(bar.style.left));
    expect(lefts).toEqual([...lefts].sort((a, b) => a - b));
    expect(new Set(bars.map(bar => bar.style.width)).size).toBe(1);
    screen.unmount();
  });

  it('orders the runs by a column when its header is pressed, and turns the order over on a second press', () => {
    const screen = mount(page(views.week));
    const first = firstRun(screen);
    screen.press(screen.control('Turns'));
    const byTurns = firstRun(screen);
    screen.press(screen.control('Turns'));
    expect(firstRun(screen)).not.toBe(byTurns);
    screen.press(screen.control('Cost'));
    screen.press(screen.control('Cost'));
    screen.press(screen.control('Cost'));
    expect(firstRun(screen)).toBe(first);
    screen.unmount();
  });

  it('opens a run from its row', () => {
    const opened: string[] = [];
    const screen = mount(page(views.week, opened));
    const [row] = screen.container.querySelectorAll<HTMLElement>('tr[data-selectable]');
    if (row === undefined) throw new Error('no run listed');
    screen.press(row);
    expect(opened).toEqual([`host-a/${views.week.runs[0]?.runId}`]);
    screen.unmount();
  });

  it("keeps a run's name whole for the pointer while the layout cuts it", () => {
    const screen = mount(page(views.week));
    const cell = screen.container.querySelector('tr[data-selectable] td[data-clip]');
    expect(cell?.getAttribute('title')).toBe(cell?.textContent);
    screen.unmount();
  });

  it('writes no protocol word, NaN or empty value on any state', () => {
    for (const view of [undefined, views.none, views.short, views.week]) {
      const screen = mount(page(view));
      expect(screen.text()).not.toMatch(/UNSPECIFIED|undefined|NaN|\[object|null/);
      screen.unmount();
    }
  });
});
