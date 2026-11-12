/**
 * The run report through the DOM, over served views: the search view with its series, trade-off and
 * bounds, the lead's reports and the cost view, as the host would send them. Nothing here works out a
 * best, a frontier or a cost; each test says what the host sent and reads what a person sees.
 */

import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  BoundUnitCheck,
  CommandSchema,
  Direction,
  ObjectiveStandingSchema,
  oneofIs,
  PlanStatus,
  ReceiptSchema,
  ReportKind,
  SearchStateSchema,
} from '@vibesys/orchestrator-proto';
import {Chip} from '@vibesys/ui';
import fc from 'fast-check';
import {act} from 'react';
import {
  applyChange,
  applyCost,
  applySearch,
  type CommandProgress,
  type CostFeedSnapshot,
  type CostView,
  emptyState,
  type IdeaWindow,
  type OrchestratorState,
  objectiveReports,
  type RunView,
  runReport,
} from '../../../state/index.js';
import {typeInto} from '../../../testing/dom/fields.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {NO_PRICES, PRICED_AND_UNPRICED} from '../../../testing/orchestrator/cost-views.js';
import {
  generateScenario,
  type MeasuredOptions,
  measuredSearch,
  type ObjectiveSpec,
  SERVING,
  serveSummaries,
  withReport,
} from '../../../testing/orchestrator/index.js';
import {FLOOR_HINT} from '../../cost/index.js';
import {runTitle} from '../fleet-words.js';
import {agentNames} from '../names.js';
import {formatFigure} from './format.js';
import {ObjectiveSummary} from './objective-summary.js';
import {PerformancePanel} from './performance.js';
import {PlansSummary} from './plans-summary.js';
import {ReportScreen} from './report-screen.js';

installDom();

const seed = 4;
const base = generateScenario({seed, subagents: 5, live: true});
const RUN = base.runId;
const START = Date.UTC(2026, 10, 1, 17, 0, 0);
const END = base.endMs;
const REPORT_AT = END;
const NOW = END + 23 * 60_000;

/** The cost views the host would send, for this run. */
const COST: CostView = {...PRICED_AND_UNPRICED, runId: RUN};
const NO_PRICE: CostView = {...NO_PRICES, runId: RUN};

interface World {
  readonly objectives?: readonly ObjectiveSpec[];
  readonly candidates?: number;
  readonly report?: boolean;
  readonly cost?: CostView | null;
  readonly revisedBound?: boolean;
  readonly live?: boolean;
  readonly stale?: boolean;
  readonly ideas?: number;
  readonly truncated?: boolean;
  readonly window?: IdeaWindow | null;
  /** The lead's report is a note along the way even when the run is over. */
  readonly finalReport?: boolean;
  /** The plans were made by the agents the run has, so the Agent column names them. */
  readonly named?: boolean;
  /** When the screen is drawn; the end of the scenario and some minutes when absent. */
  readonly nowMs?: number;
}

function stateOf(world: World): OrchestratorState {
  const scenario =
    world.live === false ? generateScenario({seed, subagents: 5, live: false}) : base;
  const withText =
    world.report === false
      ? scenario
      : withReport(scenario, {
          sequence: 2,
          kind:
            world.live === false && world.finalReport !== false
              ? ReportKind.FINAL
              : ReportKind.PROGRESS,
          atMs: REPORT_AT,
          headline: 'Throughput is 2.3 times the baseline at p99 118 ms.',
          changed: 'Fused expert dispatch landed.',
        });
  const options: MeasuredOptions = {
    seed,
    candidates: world.candidates ?? 40,
    startMs: START,
    endMs: END,
    objectives: world.objectives ?? SERVING,
    ...(world.revisedBound === undefined ? {} : {revisedBound: world.revisedBound}),
    ...(world.ideas === undefined ? {} : {ideas: world.ideas}),
    ...(world.named === true
      ? {
          agents: Object.values(withText.changes.reduce(applyChange, emptyState).agents).map(
            a => a.id,
          ),
        }
      : {}),
  };
  let state = withText.changes.reduce(applyChange, emptyState);
  const search = measuredSearch(RUN, options, {stale: world.stale === true});
  state = applySearch(
    state,
    world.truncated === true ? create(SearchStateSchema, {...search, truncated: true}) : search,
  );
  const cost = world.cost === undefined ? COST : world.cost;
  return serveSummaries(cost === null ? state : applyCost(state, cost));
}

function render(
  world: World,
  options: {
    costFeed?: CostFeedSnapshot;
    stopProgress?: CommandProgress;
  } = {},
) {
  const probe = {shown: [] as RunView[], stops: 0, runs: 0, catchUps: 0, again: 0};
  const state = stateOf(world);
  const nowMs = world.nowMs ?? (world.live === false ? END + 60_000 : NOW);
  const report = runReport(state, RUN, nowMs);
  if (report === undefined) throw new Error('the fixture has its run');
  const screen = mount(
    <ReportScreen
      report={report}
      host="host-a"
      ideasWindow={world.window ?? null}
      nowMs={nowMs}
      cost={state.costs[RUN]}
      costFeed={options.costFeed ?? {status: {_tag: 'Live'}, freshness: 'live'}}
      retryCost={() => undefined}
      stopProgress={options.stopProgress}
      stop={() => {
        probe.stops += 1;
      }}
      retryStop={() => undefined}
      openCatchUp={() => {
        probe.catchUps += 1;
      }}
      runAgain={() => {
        probe.again += 1;
      }}
      goRuns={() => {
        probe.runs += 1;
      }}
      openAgents={() => probe.shown.push('agents')}
      openCost={() => probe.shown.push('cost')}
      names={agentNames(state, RUN)}
      reading={null}
    />,
  );
  return {screen, probe};
}

/** The text of the card with this heading, or an empty text. */
const cardText = (root: HTMLElement, heading: string): string =>
  [...root.querySelectorAll('section.vs-card')].find(
    card => card.querySelector('h2')?.textContent === heading,
  )?.textContent ?? '';

const cards = (root: HTMLElement): string[] =>
  [...root.querySelectorAll('section.vs-card h2')].map(heading => heading.textContent ?? '');

/** The facts of the facts line, one string each, a bar between them. */
const factsOf = (root: HTMLElement): string =>
  [...root.querySelectorAll('.vs-factline .vs-fact')].map(fact => fact.textContent).join(' | ');

/** The charts of the performance panel: one per band, and not the trade-off's. */
const bandCharts = (root: HTMLElement): SVGElement[] =>
  [...root.querySelectorAll<SVGElement>('svg.vs-chart')].filter(svg =>
    svg.getAttribute('aria-label')?.includes('over time'),
  );

/** The cells of the rows of a table with this name. */
const rowsOf = (root: HTMLElement, name: string): string[][] =>
  [...(root.querySelector(`table[aria-label="${name}"]`)?.querySelectorAll('tbody tr') ?? [])].map(
    row => [...row.querySelectorAll('td')].map(cell => cell.textContent ?? ''),
  );

describe('the report as a page of the run', () => {
  it('opens on the trail, then the title, with Catch me up and Stop run in one group and no best line', () => {
    const {screen} = render({});
    const header = screen.container.querySelector('.vs-page-header');
    expect(header?.firstElementChild?.classList.contains('vs-page-trail')).toBe(true);
    const run = stateOf({}).runs[RUN];
    expect(header?.querySelector('h1')?.textContent).toBe(
      run === undefined ? 'a title' : runTitle(run.taskCard ?? null, run.task),
    );
    // What is best so far is the performance panel's, so the header does not say it a second time.
    expect(header?.textContent).not.toMatch(/\bBest\b/);
    const actions = [...(screen.container.querySelectorAll('.vs-page-actions .vs-button') ?? [])];
    expect(actions.map(button => button.textContent)).toEqual(['Catch me up', 'Stop run']);
    expect(actions.map(button => button.getAttribute('data-size'))).toEqual(['md', 'md']);
    expect(actions.map(button => button.getAttribute('data-variant'))).toEqual([
      'primary',
      'danger',
    ]);
    screen.unmount();
  });

  it('lays the page out in one order: the facts under the title row, the lead as a row, no tiles, then the two columns', () => {
    const {screen} = render({ideas: 12});
    const root = screen.container;
    expect(root.querySelector('.vs-page-meta .vs-factline')).not.toBeNull();
    // The lead is a block with a rule on its left and not a card, and has no label column; there is no
    // row of objective tiles at all.
    const lead = root.querySelector('section.vs-brief');
    expect(lead?.textContent).toContain('Lead’s summary'.replace('’', "'"));
    expect(cards(root)).not.toContain('Lead summary');
    expect(root.querySelector('.vs-tiles, .vs-tile')).toBeNull();
    const columns = root.querySelector('.vs-mainside');
    expect(
      lead?.compareDocumentPosition(columns as Node) ?? 0 & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    const headings = (side: string): string[] =>
      [...root.querySelectorAll(`.vs-mainside-${side} section.vs-card h2`)].map(
        each => each.textContent ?? '',
      );
    expect(headings('main')).toEqual(['Performance', 'Ideas', 'Trade-off']);
    expect(headings('side')).toEqual([
      'Latest measurements',
      'Plan status',
      'Workstreams',
      'Agents and cost',
    ]);
    // Every panel is packed.
    const roomy = [...root.querySelectorAll('section.vs-card')].filter(
      card => !card.hasAttribute('data-dense'),
    );
    expect(roomy.map(card => card.textContent?.slice(0, 30))).toEqual([]);
    screen.unmount();
  });

  it('says the whole run in the facts line: what was measured and when last, the ideas, the agents and the cost', () => {
    const {screen} = render({ideas: 12});
    const facts = factsOf(screen.container);
    // Four facts and no more, each with a real space between its figure and its words, and no mark between.
    expect(facts).toMatch(
      /^40 measured \| \d+ ideas \| \d+ of \d+ agents running \| ≥\u00a0\$318 spent$/,
    );
    expect(screen.container.querySelector('.vs-factline')?.textContent).not.toContain('·');
    // The turns, what is unpriced and the age of the newest measurement are the panels'.
    expect(cardText(screen.container, 'Latest measurements')).toMatch(/latest \d+m ago/);
    expect(cardText(screen.container, 'Agents and cost')).toMatch(
      /41 of 1,433 turns reported no price\./,
    );
    screen.unmount();
    // The host cut what it served short: the fact says so on hover, and carries no mark.
    const cut = render({ideas: 12, truncated: true});
    const measured = [...cut.screen.container.querySelectorAll('.vs-fact')].find(fact =>
      fact.textContent?.includes('measured'),
    );
    expect(measured?.textContent).toBe('40 measured');
    expect(measured?.getAttribute('title')).toMatch(/only part/);
    cut.screen.unmount();
  });

  it('says nothing was measured and no ideas, and that the cost is being read, before there is any of them', () => {
    const {screen} = render({candidates: 0, cost: null});
    expect(factsOf(screen.container)).toMatch(
      /^nothing measured yet \| no ideas yet \| \d+ of \d+ agents running \| reading cost$/,
    );
    screen.unmount();
  });

  it('shows no rate of spend once the run is over, and counts the agents as the host did', () => {
    const over = render({live: false});
    expect(factsOf(over.screen.container)).toMatch(/\b\d+ of \d+ agents running/);
    expect(over.screen.text()).not.toContain('/h');
    over.screen.unmount();
  });

  it('writes the age of the report and the time the run has run in the words of the Runs list', () => {
    const {screen} = render({});
    expect(screen.container.querySelector('section.vs-brief')?.textContent).toContain('· 23m ago');
    expect(screen.container.querySelector('.vs-page-meta')?.textContent).toMatch(
      /host-afor (\d+d \d+h|\d+h \d+m|\d+m|\d+s)Started/,
    );
    expect(screen.text()).not.toMatch(/\d+ (s|min|h|d) ago/);
    screen.unmount();
  });
});

describe('the header of a report', () => {
  it('has Catch me up beside Stop while the run lives, and the press opens its page', () => {
    const {screen, probe} = render({});
    screen.press(screen.control('Catch me up'));
    expect(probe.catchUps).toBe(1);
    expect(screen.controls('Stop run')).toHaveLength(1);
    screen.unmount();
  });

  it('keeps Catch me up when the run is over, replaces Stop with the leading Run again action', () => {
    const {screen, probe} = render({live: false});
    expect(screen.controls('Catch me up')).toHaveLength(1);
    expect(screen.controls('Stop run')).toHaveLength(0);
    const again = screen.control('Run again');
    expect(again.getAttribute('data-variant')).toBe('primary');
    screen.press(again);
    expect(probe.again).toBe(1);
    screen.unmount();
  });
});

describe('the report of a run with two objectives and a declared bound', () => {
  it('shows each panel the data has, and none of an agent’s turns', () => {
    const {screen} = render({});
    expect(cards(screen.container)).toEqual([
      'Performance',
      'Ideas',
      'Trade-off',
      'Latest measurements',
      'Plan status',
      'Workstreams',
      'Agents and cost',
    ]);
    const text = screen.text();
    for (const word of ['throughput', 'p99_latency', 'Running', 'host-a', 'Started']) {
      expect(text).toContain(word);
    }
    // No transcript: nothing of the agents' prompts, thoughts or commands is on the report.
    expect(screen.container.querySelector('[role="tree"], [role="treeitem"]')).toBeNull();
    const said = Object.values(stateOf({}).items).flatMap(item =>
      oneofIs(item.body, 'assistant', {assistant: true, reasoning: true, prompt: true, tool: true})
        ? [item.body.value.text]
        : [],
    );
    expect(said.length).toBeGreaterThan(0);
    for (const line of said) expect(text).not.toContain(line.slice(0, 24));
    screen.unmount();
  });

  /** The block beside an objective's chart, found by its name. */
  const summaryOf = (root: HTMLElement, name: string): string =>
    root.querySelector(`.vs-flex [title="${name}"]`)?.parentElement?.textContent ?? '';

  it('gives each objective a band whose summary says the best’s percent of the bound exactly as the host sent it', () => {
    const {screen} = render({});
    const sent = stateOf({}).searches[RUN]?.bounds ?? [];
    expect(sent).toHaveLength(2);
    for (const bound of sent) {
      expect(summaryOf(screen.container, bound.objective)).toContain(
        `${Math.round(bound.bestPercentOfBound ?? Number.NaN)}% of bound`,
      );
    }
    screen.unmount();
  });

  it('says which way is better, and the best with its unit, and no change against the input the host does not serve', () => {
    const {screen} = render({});
    expect(summaryOf(screen.container, 'throughput')).toContain('Higher is better');
    expect(summaryOf(screen.container, 'p99_latency')).toContain('Lower is better');
    expect(summaryOf(screen.container, 'throughput')).toContain('tok/s');
    expect(screen.text()).not.toMatch(/vs input|against the input/i);
    screen.unmount();
  });

  it('shows one number for an objective’s best in its summary, whatever the head scored', () => {
    const {screen} = render({});
    const search = stateOf({}).searches[RUN];
    const objectives = search?.series ?? [];
    expect(objectives).toHaveLength(2);
    let headDiffers = 0;
    for (const series of objectives) {
      const bound = search?.bounds.find(entry => entry.objective === series.objective);
      const best = series.points.filter(point => point.bestSoFar).at(-1)?.value ?? Number.NaN;
      const figure = formatFigure(best);
      expect(summaryOf(screen.container, series.objective)).toContain(figure);
      // The head is the best of the first objective only: on the other it scored something else, and
      // that is not what the summary says.
      const head = bound?.headValue;
      if (head !== undefined && formatFigure(head) !== figure) {
        headDiffers += 1;
        expect(summaryOf(screen.container, series.objective)).not.toContain(
          `${formatFigure(head)} `,
        );
      }
    }
    expect(headDiffers).toBeGreaterThan(0);
    screen.unmount();
  });

  it('starts every chart on the time axis at one left edge, whatever its labels are', () => {
    const wide: readonly ObjectiveSpec[] = [
      {name: 'small', direction: 1 as never, unit: 'x', base: 1, best: 9},
      {name: 'large', direction: 1 as never, unit: 'x', base: 1000, best: 98_000},
    ];
    const {screen} = render({objectives: wide});
    const lefts = bandCharts(screen.container).map(svg =>
      svg.querySelector('line.vs-chart-axis')?.getAttribute('x1'),
    );
    expect(lefts).toHaveLength(2);
    expect(new Set(lefts).size).toBe(1);
    screen.unmount();
  });

  it('draws each measured candidate as a point and the best so far as a line, per objective, with a key to both', () => {
    const {screen} = render({candidates: 25});
    const charts = bandCharts(screen.container);
    expect(charts).toHaveLength(2);
    for (const chart of charts) {
      expect(chart.querySelectorAll('circle.vs-chart-point')).toHaveLength(25);
      expect(chart.querySelectorAll('path.vs-chart-step')).toHaveLength(1);
    }
    const key = [...screen.container.querySelectorAll('ul[aria-label="Key to the charts"] li')]
      .filter(item => item.getAttribute('aria-hidden') !== 'true')
      .map(item => item.textContent);
    expect(key).toEqual(['Best so far', 'Measured']);
    screen.unmount();
  });

  it('writes the value beside each new-best corner while there are 20 measurements or fewer, and not beyond', () => {
    const few = render({candidates: 12});
    for (const chart of bandCharts(few.screen.container)) {
      const corners = chart.querySelectorAll('text.vs-chart-note').length;
      expect(corners).toBeGreaterThan(0);
    }
    few.screen.unmount();
    const many = render({candidates: 21});
    for (const chart of bandCharts(many.screen.container)) {
      expect(chart.querySelectorAll('text.vs-chart-note')).toHaveLength(0);
    }
    many.screen.unmount();
  });

  it('draws a bound that is near the data with its provenance and names it in the key, and leaves a far one out', () => {
    const near: readonly ObjectiveSpec[] = [
      {name: 'throughput', direction: 1 as never, unit: 'ops/s', base: 100, best: 120, bound: 135},
    ];
    const drawn = render({objectives: near, candidates: 25});
    const chart = bandCharts(drawn.screen.container)[0];
    expect(chart?.querySelectorAll('.vs-chart-reference')).toHaveLength(1);
    const label = drawn.screen.container.querySelector('.vs-chart-reference-label');
    expect(label?.getAttribute('aria-description')).toContain('Derived from');
    const named = [
      ...drawn.screen.container.querySelectorAll('ul[aria-label="Key to the charts"] li'),
    ]
      .filter(item => item.getAttribute('aria-hidden') !== 'true')
      .map(item => item.textContent);
    expect(named).toContain('Bound');
    drawn.screen.unmount();
    const far: readonly ObjectiveSpec[] = [
      {name: 'throughput', direction: 1 as never, unit: 'ops/s', base: 100, best: 120, bound: 900},
    ];
    const left = render({objectives: far, candidates: 25});
    expect(left.screen.container.querySelectorAll('.vs-chart-reference')).toHaveLength(0);
    // The share of the bound is still said, in the summary.
    expect(left.screen.text()).toContain('% of bound');
    left.screen.unmount();
  });

  it('draws better upward on every chart: a value that is better lower is drawn higher', () => {
    const {screen} = render({candidates: 25});
    const [, latency] = bandCharts(screen.container);
    const served = stateOf({candidates: 25}).searches[RUN]?.series.find(
      series => series.objective === 'p99_latency',
    );
    const corners = (served?.points ?? []).filter(point => point.bestSoFar);
    const ys = [...(latency?.querySelectorAll('path.vs-chart-step') ?? [])].map(path =>
      [...(path.getAttribute('d') ?? '').matchAll(/V(-?[\d.]+)/g)].map(match => Number(match[1])),
    )[0];
    // The line steps up the page (a smaller y) with each new best.
    expect(corners.length).toBeGreaterThan(1);
    for (const [at, y] of (ys ?? []).entries()) {
      if (at > 0) expect(y).toBeLessThanOrEqual(ys?.[at - 1] ?? Number.POSITIVE_INFINITY);
    }
    screen.unmount();
  });

  it('marks as the frontier exactly the candidates the host marked', () => {
    const {screen} = render({candidates: 30});
    const served = stateOf({candidates: 30}).searches[RUN]?.tradeoff?.points ?? [];
    const drawn = screen.container.querySelectorAll(
      'svg.vs-chart .vs-pareto-point[data-role="frontier"]',
    );
    expect(served.some(point => point.onFrontier)).toBe(true);
    expect(drawn).toHaveLength(served.filter(point => point.onFrontier).length);
    expect(screen.container.querySelector('.vs-pareto-frontier')).not.toBeNull();
    screen.unmount();
  });

  it('puts the lead’s latest report in the lead row with its age and fields', () => {
    const {screen} = render({});
    const lead = screen.container.querySelector('section.vs-brief')?.textContent ?? '';
    expect(lead).toContain('Throughput is 2.3 times the baseline');
    expect(lead).toContain('23m ago');
    expect(lead).toContain('Fused expert dispatch landed.');
    screen.unmount();
  });

  it('lists the newest eight measurements, newest first, a value for each objective, and the new bests as the host marked them', () => {
    const {screen} = render({candidates: 40});
    const rows = rowsOf(screen.container, 'Latest measurements');
    expect(rows).toHaveLength(8);
    const headers = [
      ...(screen.container
        .querySelector('table[aria-label="Latest measurements"]')
        ?.querySelectorAll('thead th') ?? []),
    ].map(cell => cell.textContent);
    // Each value column says what it is counted in, small under its name.
    expect(headers).toEqual(['Ago', 'Idea', 'throughputtok/s', 'p99_latencyms', 'Commit']);
    const served = stateOf({candidates: 40}).searches[RUN];
    const newest = [...(served?.results ?? [])].slice(-8).reverse();
    expect(rows.map(cells => cells[4])).toEqual(newest.map(result => result.commit.slice(0, 8)));
    const marked = [
      ...screen.container.querySelectorAll(
        'table[aria-label="Latest measurements"] .vs-text[data-tone="accent"]',
      ),
    ];
    const servedMarks = (served?.series ?? []).flatMap(series =>
      series.points.slice(-8).filter(point => point.bestSoFar),
    );
    expect(marked).toHaveLength(servedMarks.length);
    // The key is the words "New best" in the style the table gives a new best, and no number.
    const latest = [...screen.container.querySelectorAll('section.vs-card')].find(
      card => card.querySelector('h2')?.textContent === 'Latest measurements',
    );
    const key = [...(latest?.querySelectorAll('.vs-text[data-tone="accent"]') ?? [])].filter(
      each => each.closest('table') === null,
    );
    expect(key.map(each => each.textContent)).toEqual(['New best']);
    screen.unmount();
  });

  it('counts the agents by type, how many are working of how many it has had, beside the cost of the type', () => {
    const {screen} = render({});
    const rows = rowsOf(screen.container, 'Agents and cost by type');
    expect(rows.length).toBeGreaterThan(0);
    for (const cells of rows) {
      expect(cells[1]).toMatch(/^\d+ of \d+$/);
      expect(cells[5]).toMatch(/^(≥\s)?(\$[\d,.]+|unknown|—)$/);
    }
    expect(rows.map(cells => cells[0])).toContain('Worker');
    screen.unmount();
  });
});

describe('the report’s links and controls', () => {
  it('open the agents and the cost by intent, return to the runs, and stop a live run', () => {
    const {screen, probe} = render({});
    screen.press(screen.control('Open agents'));
    screen.press(screen.control('Open cost'));
    expect(probe.shown).toEqual(['agents', 'cost']);
    const runs = screen.controls('Runs').find(control => control.classList.contains('vs-crumb'));
    if (runs === undefined) throw new Error('no Runs breadcrumb');
    screen.press(runs);
    screen.press(screen.control('Stop run'));
    expect({runs: probe.runs, stops: probe.stops}).toEqual({runs: 1, stops: 1});
    screen.unmount();
  });
});

describe('the report with a different amount of data', () => {
  it('has no trade-off with one objective, and its chart is the only one', () => {
    const {screen} = render({objectives: SERVING.slice(0, 1)});
    expect(cards(screen.container)).not.toContain('Trade-off');
    expect(bandCharts(screen.container)).toHaveLength(1);
    screen.unmount();
  });

  it('says one candidate, not one candidates, for a trade-off with a single measured point', () => {
    const {screen} = render({candidates: 1});
    expect(cardText(screen.container, 'Trade-off')).toContain('1 candidate ·');
    expect(cardText(screen.container, 'Trade-off')).not.toContain('1 candidates');
    screen.unmount();
  });

  it('with three objectives picks the pair to plot, and joins the frontier on none', () => {
    const three: readonly ObjectiveSpec[] = [
      ...SERVING,
      {name: 'memory', direction: 1 as never, unit: 'GB', base: 61, best: 58},
    ];
    const {screen} = render({objectives: three});
    const selects = screen.container.querySelectorAll('select');
    expect(selects).toHaveLength(2);
    expect(screen.container.querySelector('.vs-pareto-frontier')).toBeNull();
    screen.unmount();
  });

  it('keeps every panel in its place with nothing measured, each with its frame and one line of what it waits for', () => {
    const {screen} = render({candidates: 0, ideas: 0});
    const root = screen.container;
    expect(cards(root)).toEqual([
      'Performance',
      'Ideas',
      'Latest measurements',
      'Plan status',
      'Workstreams',
      'Agents and cost',
    ]);
    // A chart frame with the run's time axis, and why there is nothing in it.
    const frames = root.querySelectorAll('svg.vs-chart .vs-chart-frame');
    expect(frames.length).toBeGreaterThan(0);
    expect(root.querySelector('.vs-chart-state')?.textContent).toMatch(
      /^Waiting for the input's measurement · started \d+[smhd]/,
    );
    expect(root.querySelectorAll('svg.vs-chart text.vs-chart-label').length).toBeGreaterThan(1);
    // The tables keep their headers and say it in one row.
    expect(rowsOf(root, 'Idea list')).toEqual([
      ['No ideas yet · they appear here as agents propose them'],
    ]);
    expect(rowsOf(root, 'Latest measurements')).toEqual([['No measurements yet']]);
    expect(rowsOf(root, 'Workstream list')).toEqual([['No workstreams yet']]);
    const headers = (name: string): (string | null)[] =>
      [...root.querySelectorAll(`table[aria-label="${name}"] thead th`)].map(
        each => each.textContent,
      );
    expect(headers('Idea list')).toEqual([
      '#',
      'Idea',
      'Workstream',
      'Verdict',
      'Gain',
      'Agent',
      'Updated',
    ]);
    expect(root.querySelector('[role="alert"]')).toBeNull();
    screen.unmount();
  });

  it('holds one layout for any amount of data: the same panels in the same order, and a chart as tall as its band is', () => {
    const spec = fc.record({
      candidates: fc.constantFrom(0, 1, 3, 20, 21, 60, 400),
      objectives: fc.integer({min: 1, max: 3}),
      ideas: fc.constantFrom(0, 12),
      live: fc.boolean(),
    });
    const all: readonly ObjectiveSpec[] = [
      ...SERVING,
      {name: 'memory', direction: 2 as never, unit: 'GB', base: 61, best: 58, bound: 40},
    ];
    fc.assert(
      fc.property(spec, data => {
        const {screen} = render({
          objectives: all.slice(0, data.objectives),
          candidates: data.candidates,
          ideas: data.ideas,
          live: data.live,
        });
        const panels = cards(screen.container).filter(heading => heading !== 'Trade-off');
        expect(panels).toEqual([
          'Performance',
          'Ideas',
          'Latest measurements',
          'Plan status',
          'Workstreams',
          'Agents and cost',
        ]);
        // 200 px for one objective, 150 px each for several, whatever the number of points.
        const performance = [...screen.container.querySelectorAll('section.vs-card')].find(
          card => card.querySelector('h2')?.textContent === 'Performance',
        );
        const heights = [...(performance?.querySelectorAll('svg.vs-chart') ?? [])].map(svg =>
          svg.getAttribute('height'),
        );
        expect(heights).toHaveLength(data.objectives);
        expect(new Set(heights)).toEqual(new Set([data.objectives === 1 ? '200' : '150']));
        screen.unmount();
      }),
      {numRuns: 30, seed: 4051},
    );
  });

  it('shows no bound and no share of one when none was declared', () => {
    const {screen} = render({objectives: SERVING.map(({bound: _, ...o}) => o)});
    expect(screen.container.querySelectorAll('.vs-chart-reference-label')).toHaveLength(0);
    expect(screen.container.querySelectorAll('.vs-chart-reference')).toHaveLength(0);
    expect(screen.text()).not.toContain('% of bound');
    screen.unmount();
  });

  it('says only that the lead has not reported when it has posted none, in one quiet line with no label', () => {
    const {screen} = render({report: false});
    expect(screen.container.querySelector('section.vs-brief')).toBeNull();
    expect(screen.text()).toContain('The lead has not reported yet');
    expect(screen.container.querySelector('.vs-brief-field')).toBeNull();
    screen.unmount();
  });

  it('for an ended run has no Stop, counts the time to its end, and calls a final report final', () => {
    const {screen} = render({live: false});
    expect(screen.controls('Stop run')).toHaveLength(0);
    expect(screen.text()).toContain('Finished');
    expect(screen.container.querySelector('section.vs-brief')?.textContent).toContain(
      'final summary',
    );
    screen.unmount();
  });

  it('says what an ended run with nothing measured, did not measure, rather than that it waits', () => {
    const {screen} = render({live: false, candidates: 0});
    expect(screen.container.querySelector('.vs-chart-state')?.textContent).toBe(
      'Nothing was measured',
    );
    screen.unmount();
  });

  it('names the run in a stop outcome while it lives and drops the outcome once it is over', () => {
    const accepted: CommandProgress = {_tag: 'Accepted', receipt: create(ReceiptSchema, {})};
    const live = render({}, {stopProgress: accepted});
    expect(live.screen.text()).toContain('Stop run: accepted');
    live.screen.unmount();
    const over = render({live: false}, {stopProgress: accepted});
    expect(over.screen.text()).not.toContain('Stop run: accepted');
    over.screen.unmount();
  });

  it('shows no line for a Stop, whatever its outcome, once the run is over', () => {
    const receipt = create(ReceiptSchema, {});
    const command = create(CommandSchema, {});
    const fault = {kind: 'Unavailable', detail: 'gone', retryable: true} as const;
    const outcomes: readonly CommandProgress[] = [
      {_tag: 'Sending', command},
      {_tag: 'Accepted', receipt},
      {_tag: 'Refused', receipt},
      {_tag: 'Unconfirmed', command, fault},
      {_tag: 'Failed', command, fault},
    ];
    for (const progress of outcomes) {
      const over = render({live: false}, {stopProgress: progress});
      expect(over.screen.text()).not.toContain('Stop run:');
      over.screen.unmount();
    }
  });

  it('drops "yet" from what an ended run has none of, since no more will come, and keeps it while the run lives', () => {
    for (const live of [true, false]) {
      const {screen} = render({live, candidates: 0, ideas: 0});
      const yet = live ? ' yet' : '';
      expect(factsOf(screen.container)).toContain(`nothing measured${yet} | no ideas${yet} |`);
      expect(cardText(screen.container, 'Ideas')).toContain(
        live ? 'No ideas yet · they appear here as agents propose them' : 'No ideas',
      );
      expect(cardText(screen.container, 'Workstreams')).toContain(`No workstreams${yet}`);
      if (!live)
        expect(screen.text()).not.toMatch(
          /No (ideas|workstreams|agents) yet|nothing measured yet/i,
        );
      screen.unmount();
    }
  });

  it('shows a dash for a cost no turn priced, and says the others are floors', () => {
    const none = render({cost: NO_PRICE});
    // Turns ran and none was priced: unknown, never zero; a dash is only for a type with no turn yet.
    expect(factsOf(none.screen.container)).toContain('unknown spent');
    for (const cells of rowsOf(none.screen.container, 'Agents and cost by type')) {
      expect(cells[5]).toBe(cells[2] === '0' ? '—' : 'unknown');
    }
    none.screen.unmount();
    const floor = render({});
    expect(cardText(floor.screen.container, 'Agents and cost')).toMatch(/at least this/);
    // The spend on the facts line is a floor too, marked and explained as the Cost page does.
    const spent = [...floor.screen.container.querySelectorAll('.vs-factline .vs-fact')].find(each =>
      each.textContent?.endsWith('spent'),
    );
    expect(spent?.textContent).toContain('≥');
    expect(spent?.getAttribute('title')).toBe(FLOOR_HINT);
    floor.screen.unmount();
  });

  it('says the cost is being read until the host has sent it, and why it failed when it did', () => {
    const reading = render({cost: null});
    expect(cardText(reading.screen.container, 'Agents and cost')).toContain('Reading cost');
    reading.screen.unmount();
    const failed = render(
      {cost: null},
      {
        costFeed: {
          status: {_tag: 'Failed', fault: {kind: 'Unavailable', detail: '', retryable: true}},
          freshness: 'unavailable',
        },
      },
    );
    expect(cardText(failed.screen.container, 'Agents and cost')).toContain('Unreachable');
    expect(failed.screen.controls('Retry cost')).toHaveLength(1);
    failed.screen.unmount();
  });
});

describe('a run long enough to need a range', () => {
  const HOUR = 3_600_000;
  const long = {nowMs: START + 30 * HOUR};

  it('offers the last hour, six hours, a day and all of it, with the whole run chosen, and an overview to drag', () => {
    const {screen} = render(long);
    const segments = [...screen.container.querySelectorAll('[aria-label="Range"] label')].map(
      each => each.textContent,
    );
    expect(segments).toEqual(['1h', '6h', '1d', 'All']);
    const chosen = screen.container.querySelector<HTMLInputElement>(
      '[aria-label="Range"] input:checked',
    );
    expect(chosen?.value).toBe('all');
    expect(screen.container.querySelector('[role="slider"]')?.getAttribute('aria-valuetext')).toBe(
      'Whole run',
    );
    expect(screen.text()).toContain('Drag to zoom');
    screen.unmount();
  });

  it('offers none of it for a run that is not long, and the whole run is all there is', () => {
    const {screen} = render({nowMs: START + 5 * HOUR});
    expect(screen.container.querySelector('[aria-label="Range"]')).toBeNull();
    expect(screen.container.querySelector('[role="slider"]')).toBeNull();
    screen.unmount();
  });

  it('shows how many measurements are in view once a window is chosen, and keeps the charts’ height', () => {
    const {screen} = render(long);
    const before = bandCharts(screen.container).map(svg => svg.getAttribute('height'));
    const hour = [...screen.container.querySelectorAll('[aria-label="Range"] label')].find(
      each => each.textContent === '1h',
    );
    act(() => hour?.querySelector<HTMLInputElement>('input')?.click());
    expect(screen.text()).toMatch(/\d+ of 40 in view/);
    expect(bandCharts(screen.container).map(svg => svg.getAttribute('height'))).toEqual(before);
    expect(screen.container.querySelector('[role="slider"]')?.getAttribute('aria-valuetext')).toBe(
      'Last hour',
    );
    // The measurements are long past: the window says so instead of drawing nothing in silence.
    expect(screen.container.querySelector('.vs-chart-state')?.textContent).toBe(
      'Nothing measured in this range',
    );
    screen.unmount();
  });
});

describe('what the header says about the host’s data', () => {
  it('says the search state is stale in the header, not as a banner', () => {
    const {screen} = render({stale: true});
    expect(screen.text()).toContain('Search stale');
    expect(screen.container.querySelector('[role="alert"], .vs-banner')).toBeNull();
    screen.unmount();
  });

  it('puts the words it is given beside the title once, and adds none of its own about the link', () => {
    const state = stateOf({});
    const report = runReport(state, RUN, NOW);
    if (report === undefined) throw new Error('the fixture has its run');
    const screen = mount(
      <ReportScreen
        report={report}
        host="host-a"
        ideasWindow={null}
        nowMs={NOW}
        cost={state.costs[RUN]}
        costFeed={{status: {_tag: 'Live'}, freshness: 'live'}}
        retryCost={() => undefined}
        stopProgress={undefined}
        stop={() => undefined}
        retryStop={() => undefined}
        openCatchUp={() => undefined}
        runAgain={() => undefined}
        goRuns={() => undefined}
        openAgents={() => undefined}
        openCost={() => undefined}
        names={agentNames(state, RUN)}
        reading={<Chip label="Stale 1m" tone="warn" dot />}
      />,
    );
    const chips = [...screen.container.querySelectorAll('.vs-page-meta .vs-chip')].map(
      each => each.textContent,
    );
    expect(chips.filter(text => text === 'Stale 1m')).toHaveLength(1);
    screen.unmount();
  });
});

describe('the report across any data', () => {
  const spec = fc.record({
    candidates: fc.integer({min: 0, max: 60}),
    objectives: fc.integer({min: 1, max: 3}),
    bounded: fc.boolean(),
    report: fc.boolean(),
    priced: fc.boolean(),
    live: fc.boolean(),
  });

  it('renders, names nothing from the protocol, and shows one chart per objective', () => {
    const all: readonly ObjectiveSpec[] = [
      ...SERVING,
      {name: 'memory', direction: 2 as never, unit: 'GB', base: 61, best: 58, bound: 40},
    ];
    fc.assert(
      fc.property(spec, data => {
        const objectives = all
          .slice(0, data.objectives)
          .map(({bound, ...rest}) =>
            data.bounded && bound !== undefined ? {...rest, bound} : rest,
          );
        const {screen} = render({
          objectives,
          candidates: data.candidates,
          report: data.report,
          live: data.live,
          cost: data.priced ? COST : NO_PRICE,
        });
        expect(screen.text()).not.toMatch(/UNSPECIFIED|undefined|NaN|\[object|null/);
        const performance = [...screen.container.querySelectorAll('section.vs-card')].find(
          card => card.querySelector('h2')?.textContent === 'Performance',
        );
        const charts = performance?.querySelectorAll('svg.vs-chart') ?? [];
        // A chart for each objective; with none declared yet, one for the measurements to come.
        const bands = data.candidates === 0 && !data.bounded ? 1 : data.objectives;
        expect(charts).toHaveLength(bands);
        screen.unmount();
      }),
      {numRuns: 25, seed: 4050},
    );
  });
});

describe('the ideas and workstreams of the report', () => {
  const table = (root: HTMLElement, name: string): HTMLTableElement | null =>
    root.querySelector(`table[aria-label="${name}"]`);

  /** Chooses the segment with this label, as a person does. */
  const choose = (root: HTMLElement, label: string): void => {
    const segment = [...root.querySelectorAll('label.vs-segment')].find(
      each => each.textContent === label,
    );
    const input = segment?.querySelector<HTMLInputElement>('input');
    if (input === null || input === undefined) throw new Error(`no segment ${label}`);
    act(() => input.click());
  };

  const VERDICT = {1: 'Open', 2: 'Kept', 3: 'Rejected', 4: 'Parked'} as const;

  it('shows the ideas the run worked on by default, each with its served verdict and gain, updated newest first', () => {
    const {screen} = render({ideas: 12});
    const served = stateOf({ideas: 12}).searches[RUN];
    const inRun = (served?.ideas ?? []).filter(view => view.inRun);
    expect(inRun.length).toBeGreaterThan(0);
    expect(inRun.length).toBeLessThan(12);
    const rows = rowsOf(screen.container, 'Idea list');
    expect(rows).toHaveLength(inRun.length);
    for (const view of inRun) {
      const row = rows.find(cells => cells[0] === `#${view.planNumber}`);
      expect(row?.[3]).toBe(VERDICT[view.verdict as 1 | 2 | 3 | 4]);
      const gain = view.best[0];
      expect(row?.[4]).toBe(gain === undefined ? '—' : `${gain.ratio.toFixed(2)}×`);
    }
    // Newest update first: here a later plan is a later update.
    const numbers = rows.map(cells => Number(cells[0]?.slice(1)));
    expect(numbers).toEqual([...numbers].sort((a, b) => b - a));
    screen.unmount();
  });

  it('counts each verdict on its chip, with the verdict’s color, and shows only that verdict when it is pressed', () => {
    const {screen} = render({ideas: 12});
    const served = stateOf({ideas: 12}).searches[RUN];
    const inRun = (served?.ideas ?? []).filter(view => view.inRun);
    const chip = (word: string): HTMLButtonElement => {
      const found = [
        ...screen.container.querySelectorAll<HTMLButtonElement>('button.vs-dotlegend-item'),
      ].find(each => each.textContent?.startsWith(word));
      if (found === undefined) throw new Error(`no chip ${word}`);
      return found;
    };
    expect(chip('All').textContent).toBe(`All${inRun.length}`);
    for (const [number, word] of Object.entries(VERDICT)) {
      const count = inRun.filter(view => view.verdict === Number(number)).length;
      expect(chip(word).textContent).toBe(`${word}${count}`);
      expect(chip(word).hasAttribute('disabled')).toBe(count === 0);
    }
    const kept = inRun.filter(view => view.verdict === 2).length;
    screen.press(chip('Kept'));
    const rows = rowsOf(screen.container, 'Idea list');
    expect(rows).toHaveLength(kept);
    for (const cells of rows) expect(cells[3]).toBe('Kept');
    // Pressing it again shows every idea again.
    screen.press(chip('Kept'));
    expect(rowsOf(screen.container, 'Idea list')).toHaveLength(inRun.length);
    screen.unmount();
  });

  it('finds an idea by the words in its row, and says when none match', () => {
    const {screen} = render({ideas: 12});
    const box = screen.container.querySelector<HTMLInputElement>('input[type="search"]');
    if (box === null) throw new Error('no search field');
    typeInto(box, 'fp8');
    const rows = rowsOf(screen.container, 'Idea list');
    expect(rows.length).toBeGreaterThan(0);
    for (const cells of rows) expect(cells[1]?.toLowerCase()).toContain('fp8');
    typeInto(box, 'no idea is called this');
    expect(rowsOf(screen.container, 'Idea list')).toEqual([['No ideas match']]);
    screen.unmount();
  });

  it('sorts by a header, and turns the order over when it is pressed again', () => {
    const {screen} = render({ideas: 12});
    const header = (name: string): HTMLButtonElement => {
      const found = [...screen.container.querySelectorAll<HTMLButtonElement>('th button')].find(
        each => each.textContent === name,
      );
      if (found === undefined) throw new Error(`no header ${name}`);
      return found;
    };
    const titles = (): string[] =>
      rowsOf(screen.container, 'Idea list').map(cells => cells[1] ?? '');
    screen.press(header('Idea'));
    expect(titles()).toEqual(
      [...titles()].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase())),
    );
    expect(header('Idea').closest('th')?.getAttribute('aria-sort')).toBe('ascending');
    screen.press(header('Idea'));
    expect(header('Idea').closest('th')?.getAttribute('aria-sort')).toBe('descending');
    expect(titles()).toEqual(
      [...titles()].sort((a, b) => b.toLowerCase().localeCompare(a.toLowerCase())),
    );
    screen.unmount();
  });

  it('cuts a long title to its column and says it whole on hover', () => {
    const {screen} = render({ideas: 12});
    choose(screen.container, 'All ideas 12');
    const long = [...screen.container.querySelectorAll('td[data-clip]')].find(cell =>
      cell.getAttribute('title')?.startsWith('A speculative draft reuse'),
    );
    expect(long?.getAttribute('title')?.length).toBeGreaterThan(60);
    expect(long?.textContent).toBe(long?.getAttribute('title'));
    screen.unmount();
  });

  it('shows the workstreams as compact rows: the host’s counts as a bar, their total beside it, and the best gain', () => {
    const {screen} = render({ideas: 12});
    const served = stateOf({ideas: 12}).searches[RUN];
    const rows = rowsOf(screen.container, 'Workstream list');
    expect(rows).toHaveLength(served?.workstreamViews.length ?? -1);
    const bars = [
      ...(table(screen.container, 'Workstream list')?.querySelectorAll('tbody tr') ?? []),
    ].map(row =>
      [...row.querySelectorAll('.vs-share-segment')].map(segment => segment.getAttribute('title')),
    );
    served?.workstreamViews.forEach((view, at) => {
      for (const [word, count] of [
        ['Kept', view.kept],
        ['Rejected', view.rejected],
        ['Open', view.open],
        ['Parked', view.parked],
      ] as const) {
        if (count > 0) expect(bars[at]).toContain(`${word} ${count}`);
      }
    });
    // The count of the ideas is a number beside the bar, the host's counts added.
    served?.workstreamViews.forEach((view, at) => {
      const total = view.kept + view.rejected + view.open + view.parked;
      expect(rows[at]?.[1]).toBe(String(total));
    });
    expect(cardText(screen.container, 'Workstreams')).toContain('No workstream');
    // The colors of the bars are named by a key, and are the colors the Ideas chips give the same verdicts.
    const tones = (heading: string): Map<string, string | null> =>
      new Map(
        [
          ...([...screen.container.querySelectorAll('section.vs-card')]
            .find(card => card.querySelector('h2')?.textContent === heading)
            ?.querySelectorAll('.vs-dotlegend-item') ?? []),
        ].map(item => [
          item.querySelector('.vs-text')?.textContent ?? '',
          item.querySelector('.vs-dot')?.getAttribute('data-tone') ?? null,
        ]),
      );
    const ofBars = tones('Workstreams');
    const ofChips = tones('Ideas');
    expect([...ofBars.keys()]).toEqual(['Kept', 'Rejected', 'Open', 'Parked']);
    for (const [verdict, tone] of ofBars) expect(ofChips.get(verdict)).toBe(tone);
    const segments = [...screen.container.querySelectorAll<HTMLElement>('.vs-share-segment')].map(
      each => each.style.getPropertyValue('--seg-color'),
    );
    for (const color of segments) {
      expect(['var(--ok)', 'var(--muted)', 'var(--accent)', 'var(--alt)']).toContain(color);
    }
    screen.unmount();
  });

  it('says how many candidates name no plan, and marks a state the host cut short, in a label and not a banner', () => {
    const {screen} = render({ideas: 12, truncated: true});
    const served = stateOf({ideas: 12}).searches[RUN];
    // What the count is of is said: measurements that had no plan.
    expect(cardText(screen.container, 'Ideas')).toContain(
      `${served?.unplanned} measurements had no plan`,
    );
    expect(cardText(screen.container, 'Ideas')).toContain('Some left out');
    expect(screen.container.querySelector('[role="alert"]')).toBeNull();
    screen.unmount();
    const whole = render({ideas: 12});
    expect(cardText(whole.screen.container, 'Ideas')).not.toContain('Some left out');
    whole.screen.unmount();
  });

  it('has an empty state for a run with no ideas', () => {
    const {screen} = render({ideas: 0});
    expect(cardText(screen.container, 'Ideas')).toContain('No ideas yet');
    expect(cardText(screen.container, 'Workstreams')).toContain('No workstreams');
    screen.unmount();
  });

  it('opens narrowed to the check-in window it is given, and still shows every idea on All', () => {
    const served = stateOf({ideas: 12}).searches[RUN];
    // The check-in counted these plans, whatever the plans' own times say.
    const counted = (served?.plans ?? []).map(plan => plan.number).filter((_, at) => at % 3 === 0);
    const {screen} = render({ideas: 12, window: {plans: counted}});
    expect(rowsOf(screen.container, 'Idea list')).toHaveLength(counted.length);
    expect(screen.text()).toContain(`Check-in ${counted.length}`);
    choose(screen.container, 'All ideas 12');
    expect(rowsOf(screen.container, 'Idea list')).toHaveLength(12);
    screen.unmount();
  });

  it('links a chart point to the idea it measured by keyboard, and to its row', () => {
    const {screen} = render({ideas: 12});
    const served = stateOf({ideas: 12}).searches[RUN];
    const planned = served?.jobs.find(job => job.plans.length > 0);
    const unplanned = served?.jobs.find(job => job.plans.length === 0);
    expect(planned).toBeDefined();
    expect(unplanned).toBeDefined();
    const points = [...screen.container.querySelectorAll<SVGGElement>('.vs-chart-pick')];
    expect(points.length).toBeGreaterThan(0);
    // Every point is on the tab order's one stop, and an arrow key moves among them.
    expect(
      points.filter(point => point.getAttribute('tabindex') === '0').length,
    ).toBeLessThanOrEqual(2);
    const labelOf = (jobId: string | undefined): string | undefined => {
      const commit = served?.results.find(result => result.jobId === jobId)?.commit.slice(0, 8);
      return (
        points
          .find(point => point.getAttribute('aria-label')?.startsWith(commit ?? '?'))
          ?.getAttribute('aria-label') ?? undefined
      );
    };
    expect(labelOf(unplanned?.id)).toContain('no plan');
    expect(labelOf(planned?.id)).toContain(`#${planned?.plans[0]}`);
    const none = points.find(point => point.getAttribute('aria-label') === labelOf(unplanned?.id));
    if (none === undefined) throw new Error('the unplanned point is drawn');
    act(() => {
      none.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    });
    expect(screen.container.querySelector('[aria-label="Chosen point"]')?.textContent).toContain(
      'No plan',
    );
    const linked = points.find(point => point.getAttribute('aria-label') === labelOf(planned?.id));
    if (linked === undefined) throw new Error('the planned point is drawn');
    act(() => {
      linked.dispatchEvent(new window.KeyboardEvent('keydown', {key: ' ', bubbles: true}));
    });
    const plan = planned?.plans[0];
    const show = screen.control(`Show idea ${plan}`);
    screen.press(show);
    const current = screen.container.querySelector(
      'table[aria-label="Idea list"] tr[aria-current="true"]',
    );
    expect(current?.textContent).toContain(`#${plan}`);
    screen.unmount();
  });
});

describe('the summary of an objective and the bound’s unit', () => {
  const search = create(SearchStateSchema, {
    series: [
      {
        objective: 'tok_s',
        direction: Direction.MAXIMIZE,
        points: [{jobId: 'j', commit: 'c', at: timestampFromMs(1000), value: 5, bestSoFar: true}],
      },
    ],
  });
  const summaryText = (check: BoundUnitCheck): string => {
    const standing = create(ObjectiveStandingSchema, {
      name: 'tok_s',
      bestValue: 5,
      unit: 'tok/s',
      boundUnit: 'ms',
      boundUnitCheck: check,
      bestPercentOfBound: 40,
    });
    const [objective] = objectiveReports(search, [standing]);
    if (objective === undefined) throw new Error('the series has its objective');
    const screen = mount(<ObjectiveSummary objective={objective} inView={null} runOver={false} />);
    const text = screen.text();
    screen.unmount();
    return text;
  };

  it('names both units in a short label when the bound is in another unit, and shows the measured unit beside the best', () => {
    const text = summaryText(BoundUnitCheck.MISMATCH);
    expect(text).toContain('tok/s vs bound ms');
    expect(text).toContain('tok/s');
    expect(text).not.toContain('% of bound');
  });

  it('shows the share of the bound as verified, unverified or not at all as the host checked it', () => {
    expect(summaryText(BoundUnitCheck.MATCH)).toContain('40% of bound');
    expect(summaryText(BoundUnitCheck.MATCH)).not.toContain('unverified');
    expect(summaryText(BoundUnitCheck.UNKNOWN)).toContain('40% of bound · unverified');
  });

  it('says not measured yet, with a dash for the best and no unit, before there is a measurement', () => {
    const [objective] = objectiveReports(create(SearchStateSchema, {}), []);
    expect(objective).toBeUndefined();
    const empty = mount(
      <ObjectiveSummary
        objective={{
          name: 'tok_s',
          better: 'higher',
          unit: 'tok/s',
          points: [],
          bestPath: [],
          bestValue: undefined,
          headValue: undefined,
          percentOfBound: undefined,
          bestPercentOfBound: undefined,
          bound: undefined,
          boundUnit: null,
          boundUnitCheck: BoundUnitCheck.UNSPECIFIED,
        }}
        inView={null}
        runOver={false}
      />,
    );
    expect(empty.text()).toContain('—');
    expect(empty.text()).toContain('Not measured yet');
    expect(empty.text()).not.toContain('tok/s');
    empty.unmount();
  });

  it('names an unavailable direction instead of claiming one for a summary-only objective', () => {
    const [objective] = objectiveReports(create(SearchStateSchema, {}), [
      create(ObjectiveStandingSchema, {name: 'tail latency'}),
    ]);
    if (objective === undefined) throw new Error('the summary declares its objective');
    const screen = mount(<ObjectiveSummary objective={objective} inView={null} runOver={false} />);
    expect(screen.text()).toContain('Direction unavailable');
    expect(screen.text()).not.toContain('Higher is better');
    expect(screen.text()).not.toContain('Lower is better');
    screen.unmount();
  });

  it('does not let another objective’s measurements make a summary-only objective look measured', () => {
    const objectives = objectiveReports(
      create(SearchStateSchema, {
        series: [
          {
            objective: 'throughput',
            direction: Direction.MAXIMIZE,
            points: [
              {jobId: 'j', commit: 'c', at: timestampFromMs(1000), value: 5, bestSoFar: true},
            ],
          },
        ],
      }),
      [
        create(ObjectiveStandingSchema, {name: 'throughput', bestValue: 5}),
        create(ObjectiveStandingSchema, {name: 'tail latency'}),
      ],
    );
    const screen = mount(
      <PerformancePanel
        objectives={objectives}
        span={{fromMs: 0, toMs: 10_000}}
        nowMs={20_000}
        runOver={false}
        unreadable={false}
        truncated={false}
        names={new Map()}
        links={{
          ideas: undefined,
          picked: null,
          lit: new Set(),
          pick: () => undefined,
          showIdea: () => undefined,
        }}
      />,
    );
    const states = [...screen.container.querySelectorAll('.vs-chart-state')].map(
      state => state.textContent,
    );
    expect(states).toHaveLength(1);
    expect(states[0]).toMatch(/^Waiting for the input's measurement/);
    expect(states[0]).not.toBe('Nothing measured in this range');
    screen.unmount();
  });
});

describe('the plan status of the report', () => {
  /** The word the card shows for each plan status, as a person reads it off the chip. */
  const WORD: Readonly<Record<PlanStatus, string>> = {
    [PlanStatus.UNSPECIFIED]: 'Unknown',
    [PlanStatus.OPEN]: 'Open',
    [PlanStatus.CHOSEN]: 'Chosen',
    [PlanStatus.WON]: 'Won',
    [PlanStatus.PARKED]: 'Parked',
    [PlanStatus.REJECTED]: 'Rejected',
  };

  it('counts the run’s plans by status on the page, keeping Chosen and Won apart', () => {
    const {screen} = render({ideas: 12});
    const plans = stateOf({ideas: 12}).searches[RUN]?.plans ?? [];
    expect(plans).toHaveLength(12);
    const card = cardText(screen.container, 'Plan status');
    for (const status of [
      PlanStatus.OPEN,
      PlanStatus.CHOSEN,
      PlanStatus.WON,
      PlanStatus.PARKED,
      PlanStatus.REJECTED,
    ]) {
      const count = plans.filter(plan => plan.status === status).length;
      // The fixture exercises every status, so each has a chip with the host's own count.
      expect(count).toBeGreaterThan(0);
      expect(card).toContain(`${WORD[status]} ${count}`);
    }
    // Chosen and Won are the distinction the ideas table's verdict folds into Open and Kept.
    expect(card).toContain('Chosen');
    expect(card).toContain('Won');
    screen.unmount();
  });

  it('says there are no plans for a run with none, and still appears beside the empty ideas', () => {
    const {screen} = render({ideas: 0});
    expect(cardText(screen.container, 'Plan status')).toContain('No plans');
    screen.unmount();
  });

  const plansView = (
    tallies: readonly {status: PlanStatus; label: string; count: number}[],
    withoutRun: number,
  ) => ({tallies, total: tallies.reduce((sum, tally) => sum + tally.count, 0), withoutRun});

  it('shows each status as a chip in order and says the jobs with no run, with the right plural', () => {
    const screen = mount(
      <PlansSummary
        plans={plansView(
          [
            {status: PlanStatus.OPEN, label: 'Open', count: 2},
            {status: PlanStatus.WON, label: 'Won', count: 3},
          ],
          4,
        )}
      />,
    );
    expect(
      [...screen.container.querySelectorAll('.vs-chip')].map(chip => chip.textContent),
    ).toEqual(['Open 2', 'Won 3']);
    expect(screen.text()).toContain('4 jobs with no run');
    screen.unmount();

    const one = mount(
      <PlansSummary plans={plansView([{status: PlanStatus.OPEN, label: 'Open', count: 1}], 1)} />,
    );
    expect(one.text()).toContain('1 job with no run');
    expect(one.text()).not.toContain('1 jobs with no run');
    one.unmount();
  });

  it('says there are no plans when none has been made, and still counts the jobs with no run', () => {
    const screen = mount(<PlansSummary plans={plansView([], 0)} />);
    expect(screen.text()).toContain('No plans');
    expect(screen.text()).toContain('0 jobs with no run');
    screen.unmount();
  });
});

describe('the lead’s block', () => {
  const labels = (root: HTMLElement): (string | null)[] =>
    [...root.querySelectorAll('.vs-brief-field > .vs-text')].map(each => each.textContent);

  it('has a label over each part the lead filled in, and leaves out one it left empty, label and all', () => {
    const {screen} = render({});
    // The fixture's report fills in the headline and what changed, and nothing else.
    expect(labels(screen.container)).toEqual(['Changed']);
    screen.unmount();
  });

  it('is a region named Lead summary, which the desktop end-to-end path finds the report by', () => {
    const {screen} = render({});
    const region = screen.container.querySelector('section.vs-brief[aria-label="Lead summary"]');
    expect(region?.textContent).toContain('Changed');
    screen.unmount();
  });

  it('is one column with its age once, and no text in bold or in a heading', () => {
    const {screen} = render({});
    const block = screen.container.querySelector('section.vs-brief');
    expect(block?.textContent?.match(/\d+m ago/g)).toHaveLength(1);
    expect(block?.querySelector('b, strong, h1, h2, h3, h4')).toBeNull();
    screen.unmount();
  });
});

/** Every text node of a screen, in document order. */
function textRuns(root: HTMLElement): string[] {
  const runs: string[] = [];
  const walk = (node: Node): void => {
    if (node.nodeType === 3) runs.push(node.textContent ?? '');
    for (const child of node.childNodes) walk(child);
  };
  walk(root);
  return runs;
}

describe('words that run together', () => {
  it('never glue a figure to the word after it nor split a figure ($0.10, 310.0M), on the facts line or in any panel, for any data', () => {
    const spec = fc.record({
      candidates: fc.constantFrom(0, 1, 40),
      ideas: fc.constantFrom(0, 12),
      live: fc.boolean(),
      cost: fc.constantFrom(COST, NO_PRICE, null),
      truncated: fc.boolean(),
    });
    fc.assert(
      fc.property(spec, data => {
        const {screen} = render({...data});
        const text = screen.container.textContent ?? '';
        expect(text).not.toMatch(/\d(measured|ideas|idea|agents?|spent|turns?)\b/);
        // A figure is one unbroken run of text: nothing (an element, a break) comes between a dollar
        // sign and its digits, or around a decimal point or a thousands comma.
        const runs = textRuns(screen.container).join('§');
        expect(runs).not.toMatch(/\$§|\d§[.,]\d|\d[.,]§\d/);
        screen.unmount();
      }),
      {numRuns: 20, seed: 4052},
    );
  });
});

describe('the ideas and the measurements that had no plan', () => {
  it('says so in one line when no idea was proposed but some measurements had no plan, the same count the panel gives otherwise', () => {
    const {screen} = render({candidates: 3, ideas: 0});
    expect(rowsOf(screen.container, 'Idea list')).toEqual([
      ['No ideas yet · 3 measurements had no plan'],
    ]);
    // Said once: there is no second line under the table.
    expect(cardText(screen.container, 'Ideas').match(/had no plan/g)).toHaveLength(1);
    screen.unmount();
    const some = render({ideas: 12});
    expect(cardText(some.screen.container, 'Ideas')).toMatch(/\d+ measurements had no plan/);
    expect(cardText(some.screen.container, 'Ideas')).not.toContain('unplanned');
    some.screen.unmount();
  });

  it('names the agent that made each idea as the screen names agents elsewhere', () => {
    const {screen} = render({ideas: 12, named: true});
    const names = [...new Set(rowsOf(screen.container, 'Idea list').map(cells => cells[5]))];
    expect(names.length).toBeGreaterThan(1);
    for (const name of names) expect(name).not.toBe('An agent');
    screen.unmount();
  });
});

describe('the report’s latest measurements with no row', () => {
  it('shows no key to new bests when there is no row, so no bare number reads as a best', () => {
    const {screen} = render({candidates: 0});
    expect(cardText(screen.container, 'Latest measurements')).not.toContain('New best');
    expect(cardText(screen.container, 'Latest measurements')).not.toMatch(/\d+\.\d/);
    screen.unmount();
  });
});

describe('the lead’s summary of a run that is over', () => {
  it('is not about now: the last summary of an ended run says it was before the end, and what was running was running', () => {
    const {screen} = render({live: false, finalReport: false});
    const block = screen.container.querySelector('section.vs-brief')?.textContent ?? '';
    expect(block).toContain("Lead's last summary · before the run ended");
    expect(block).not.toMatch(/\bRunning\b/);
    screen.unmount();
    const live = render({});
    expect(live.screen.container.querySelector('section.vs-brief')?.textContent).toMatch(
      /summary · \d+m ago/,
    );
    live.screen.unmount();
  });
});

describe('the cost notes', () => {
  it('count the turns that reported no price as the view says, and explain a mark only when the table has it', () => {
    const {screen} = render({});
    const note = cardText(screen.container, 'Agents and cost');
    expect(note).toContain('41 of 1,433 turns reported no price.');
    expect(note).toContain('≥ marks a cost that is at least this.');
    screen.unmount();
    const none = render({cost: NO_PRICE});
    const dashes = cardText(none.screen.container, 'Agents and cost');
    expect(dashes).toContain('9 of 9 turns reported no price.');
    expect(dashes).toContain('unknown marks a type none of whose turns was priced.');
    expect(dashes).toContain('— marks a type with no turn yet.');
    expect(dashes).not.toContain('≥ marks');
    none.screen.unmount();
  });
});

describe('where the whole history starts', () => {
  it('is marked on each chart when the host served only part of the measurements, with a hint, and not otherwise', () => {
    const cut = render({truncated: true, candidates: 40});
    const markers = cut.screen.container.querySelectorAll('svg.vs-chart .vs-chart-marker');
    expect(markers).toHaveLength(2);
    for (const marker of markers) {
      expect(marker.querySelector('text')?.textContent).toBe('all measurements from here');
      expect(marker.querySelector('title')?.textContent).toMatch(/best-so-far line/);
    }
    cut.screen.unmount();
    const whole = render({candidates: 40});
    expect(whole.screen.container.querySelector('.vs-chart-marker')).toBeNull();
    whole.screen.unmount();
  });
});

describe('the colors of verdicts and gains', () => {
  it('gives each verdict a tone of its own, Open a calm one that is no warning, and draws no gain in the color of a kept idea', () => {
    const {screen} = render({ideas: 12});
    const ideas = [...screen.container.querySelectorAll('section.vs-card')].find(
      card => card.querySelector('h2')?.textContent === 'Ideas',
    );
    const tones = [...(ideas?.querySelectorAll('button.vs-dotlegend-item') ?? [])].map(item =>
      item.querySelector('.vs-dot')?.getAttribute('data-tone'),
    );
    // All, Kept, Open, Rejected, Parked.
    expect(tones).toEqual(['neutral', 'ok', 'live', 'neutral', 'alt']);
    // Amber is a warning everywhere else (Stale, Stopping, Not read): no verdict is drawn in it.
    expect(tones).not.toContain('warn');
    const gains = screen.container.querySelectorAll(
      'table[aria-label="Idea list"] td[data-align="end"] .vs-text[data-tone="ok"], table[aria-label="Workstream list"] .vs-text[data-tone="ok"]',
    );
    expect(gains).toHaveLength(0);
    screen.unmount();
  });
});
