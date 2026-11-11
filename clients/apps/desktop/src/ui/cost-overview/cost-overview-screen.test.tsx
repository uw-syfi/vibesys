import {describe, expect, it} from '@effect/vitest';
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
  });

  it('puts the status and the facts on one line under the title, with no card of figures', () => {
    const screen = mount(page(views.short));
    const meta = screen.container.querySelector('.vs-page-meta');
    expect(meta?.querySelectorAll('.vs-factline')).toHaveLength(1);
    expect(meta?.querySelector('.vs-factline .vs-chip')?.textContent).toBe('Live');
    const text = meta?.textContent ?? '';
    expect(text).toMatch(/≥ \$2\.35\s*spent/);
    expect(text).toMatch(/in the last hour/);
    expect(text).toMatch(/1\s*run/);
    expect(text).toMatch(/9\s*turns/);
    expect(text).toContain('4 turns unpriced');
    const unpriced = [...(meta?.querySelectorAll('.vs-fact') ?? [])].find(fact =>
      fact.textContent?.includes('unpriced'),
    );
    expect(unpriced?.getAttribute('title')).toMatch(/tokens and no price/);
    expect(screen.text()).not.toContain('Last hour');
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

  it('names models by display name only: no id of a model is drawn', () => {
    const screen = mount(page(views.week));
    const text = screen.text();
    for (const id of ['claude-opus-5-5', 'gpt-5-codex', 'claude-sonnet-5-5']) {
      expect(text).not.toContain(id);
    }
    expect(text).toContain('Claude Opus 5.5');
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
