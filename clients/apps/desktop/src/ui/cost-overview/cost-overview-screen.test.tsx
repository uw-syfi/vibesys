import {describe, expect, it} from '@effect/vitest';
import {type AgentRole, Harness, RunStatus} from '@vibesys/orchestrator-proto';
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

const PANELS = ['Spend over time', 'By run', 'By agent type', 'By CLI provider and model'];
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
    expect(text).toContain('Nothing spent yet');
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
    expect(unpriced.text()).toContain('Spend unknown');
    expect(unpriced.text()).not.toContain('Nothing spent yet');
    unpriced.unmount();
  });

  it('puts the host status under the title and the figures in one band, saying each once', () => {
    const screen = mount(page(views.short));
    const meta = screen.container.querySelector('.vs-page-meta');
    expect(meta?.querySelector('.vs-chip')?.textContent).toBe('Live');
    expect(meta?.querySelectorAll('.vs-factline')).toHaveLength(0);
    const strip = screen.container.querySelector('.vs-figure-strip');
    expect(strip).not.toBeNull();
    const text = strip?.textContent ?? '';
    expect(text).toMatch(/≥\s\$2\.35/u);
    expect(text).toContain('9 finished turns');
    expect(text).toContain('4 turns unpriced');
    expect(text).not.toContain('Cost controls');
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

  it('gives every figure a label, a value and no raw number run into its words', () => {
    for (const view of [views.none, views.short, views.week]) {
      const screen = mount(page(view));
      const figures = [...screen.container.querySelectorAll('.vs-figure-strip .vs-figure')];
      expect(figures.length).toBe(6);
      for (const figure of figures) {
        const detail = figure.querySelector('.vs-figure-detail')?.textContent ?? '';
        // A figure's quiet line is words, never a figure joined to its words.
        expect(detail, detail).not.toMatch(/[\d%]\p{L}/u);
      }
      screen.unmount();
    }
  });

  it('draws the actual provider and model in the code face, with its color beside it in the table', () => {
    const screen = mount(page(views.week));
    const table = screen.container.querySelector(
      'table[aria-label="Cost by CLI provider and model on Host A"]',
    );
    const names = [...(table?.querySelectorAll('tbody td:first-child [data-mono-id]') ?? [])].map(
      each => each.textContent,
    );
    expect(names).toEqual(
      expect.arrayContaining(['Claude Code · claude-opus-5-5', 'Codex · gpt-5-codex']),
    );
    expect(table?.querySelectorAll('tbody .vs-series-swatch').length).toBe(
      table?.querySelectorAll('tbody tr').length,
    );
    expect(screen.text()).not.toContain('Claude Opus');
    screen.unmount();
  });

  it('says when the bounded model list or one of its names was shortened', () => {
    const screen = mount(page({...views.short, modelsTruncated: true}));
    expect(screen.text()).toContain(
      'Some provider and model rows are not shown because the host shortened the list or a name. Totals still include every model.',
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

const operatorUsage = (costUsd: number | null) => ({
  inputTokens: 1_000,
  cacheReadTokens: null,
  cacheCreationTokens: null,
  outputTokens: 200,
  reasoningTokens: null,
  costUsd,
});

/** A whole host view in the shape the public state API gives the screen. */
function operatorHostView(options: {
  readonly cost: number | null;
  readonly lastHour: number | null;
  readonly runs: number;
  readonly turns: number;
  readonly unpriced: number;
  readonly truncated?: boolean;
}): HostCostView {
  return {
    sequence: 1n,
    asOfMs: Date.parse('2026-11-01T19:00:00Z'),
    total: operatorUsage(options.cost),
    runCount: options.runs,
    turns: options.turns,
    unpricedTurns: options.unpriced,
    runs: [
      {
        runId: 'run-a',
        name: 'Reduce tail latency',
        status: RunStatus.RUNNING,
        maxCostUsd: 100,
        turns: options.turns,
        unpricedTurns: options.unpriced,
        usage: operatorUsage(options.cost),
        totalTokens: 1_200,
      },
    ],
    otherRuns: null,
    roles: [],
    models: [
      {
        model: {harness: Harness.CODEX, model: 'model-a'},
        turns: options.turns,
        unpricedTurns: options.unpriced,
        usage: operatorUsage(options.cost),
        totalTokens: 1_200,
      },
    ],
    series: {
      startMs: Date.parse('2026-11-01T18:00:00Z'),
      bucketMs: 3_600_000,
      buckets: [
        {
          startMs: Date.parse('2026-11-01T18:00:00Z'),
          costUsd: options.cost ?? 0,
          byModel:
            options.cost === null
              ? []
              : [
                  {
                    model: {harness: Harness.CODEX, model: 'model-a'},
                    costUsd: options.cost,
                  },
                ],
        },
      ],
    },
    lastHourUsd: options.lastHour,
    modelsTruncated: options.truncated ?? false,
  };
}

const operatorHost = (
  view: HostCostView | undefined,
  lastDayView?: HostCostView,
  workstreams = {count: 3, complete: true},
) => ({
  key: 'host-a',
  label: 'Host A',
  status: {kind: 'live' as const, cause: null, staleForMs: null},
  feed: {_tag: 'Live' as const},
  view,
  lastDayView,
  workstreams,
  titles: {},
  started: {},
  working: new Map<AgentRole, number>(),
});

describe('the Cost destination operator summary', () => {
  it('shows whole-history, last-hour and exact last-day figures as separate served facts', () => {
    const all = operatorHostView({cost: 80, lastHour: 7, runs: 1, turns: 20, unpriced: 0});
    const day = operatorHostView({cost: 12.5, lastHour: 7, runs: 1, turns: 4, unpriced: 0});
    const screen = mount(
      <CostOverviewScreen
        hosts={[operatorHost(all, day)]}
        words={COST_OVERVIEW_WORDS}
        open={() => undefined}
        retry={() => undefined}
      />,
    );

    const text = screen.text();
    for (const label of [
      'Total spend',
      'Last hour',
      'Last 24 hours',
      'Runs',
      'Workstreams',
      'Per workstream',
    ]) {
      expect(text).toContain(label);
    }
    expect(text).toContain('$80.00');
    expect(text).toContain('$7.00');
    expect(text).toContain('$12.50');
    expect(text).toContain('Codex · model-a');
    expect(text).toContain('By CLI provider and model');
    expect(text).toContain('Workstreams3');
    expect(text).toContain('Per workstream$26.67');
    expect(text).toContain('Spend ÷ workstreams');
    screen.unmount();
  });

  it('keeps the operator average distinct from attribution and provider quotas', () => {
    const all = operatorHostView({cost: 20, lastHour: 2, runs: 1, turns: 5, unpriced: 0});
    const opened: [string, string][] = [];
    const screen = mount(
      <CostOverviewScreen
        hosts={[operatorHost(all)]}
        words={COST_OVERVIEW_WORDS}
        open={(machine, runId) => opened.push([machine, runId])}
        retry={() => undefined}
      />,
    );

    const text = screen.text();
    expect(text).toContain('Workstreams3');
    expect(text).toContain('Per workstream$6.67');
    const average = screen.container.querySelector(
      '.vs-figure-strip [title*="operator-wide average"]',
    );
    expect(average?.getAttribute('title')).toContain('not attributed to a workstream');
    const row = screen.container.querySelector<HTMLElement>('tr[data-selectable]');
    if (row === null) throw new Error('no run listed');
    screen.press(row);
    expect(opened).toEqual([['host-a', 'run-a']]);
    screen.unmount();
  });

  it('marks partial totals as floors and names incomplete machine coverage', () => {
    const all = operatorHostView({
      cost: 40,
      lastHour: 4,
      runs: 1,
      turns: 9,
      unpriced: 1,
      truncated: true,
    });
    const day = operatorHostView({cost: 10, lastHour: 4, runs: 1, turns: 3, unpriced: 1});
    const screen = mount(
      <CostOverviewScreen
        hosts={[
          operatorHost(all, day),
          {
            ...operatorHost(undefined, undefined, {count: 0, complete: false}),
            key: 'host-b',
            label: 'Host B',
            status: {kind: 'unavailable', cause: null, staleForMs: null},
          },
        ]}
        words={COST_OVERVIEW_WORDS}
        open={() => undefined}
        retry={() => undefined}
      />,
    );

    expect(screen.text()).toContain('1 of 2 machines');
    expect(screen.container.querySelector('[title*="1 of 2 machines reporting"]')).not.toBeNull();
    expect(screen.text()).toContain('≥ $40.00');
    expect(screen.text()).toContain('≥ $10.00');
    expect(screen.text()).toMatch(/Workstreams≥\s3/u);
    expect(screen.text()).toContain('1 of 2 machine cost views available');
    expect(screen.text()).toContain('1 unpriced turn remains');
    expect(screen.text()).toContain('1 of 2 machines have complete workstream views');
    expect(screen.text()).toContain('Some provider and model rows are not shown');
    screen.unmount();
  });
});
