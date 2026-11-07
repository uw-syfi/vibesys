/**
 * The run report through the DOM, over served views: the search view with its series, trade-off and
 * bounds, the lead's reports and the cost view, as the host would send them. Nothing here works out a
 * best, a frontier or a cost; each test says what the host sent and reads what a person sees.
 */
import {describe, expect, it} from '@effect/vitest';
import {oneofIs, ReportKind} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  applyChange,
  applyCost,
  applySearch,
  type CostFeedSnapshot,
  type CostView,
  emptyState,
  type Freshness,
  type OrchestratorState,
  type RunView,
  runReport,
} from '../../../state/index.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {NO_PRICES, PRICED_AND_UNPRICED} from '../../../testing/orchestrator/cost-views.js';
import {
  generateScenario,
  type MeasuredOptions,
  measuredSearch,
  type ObjectiveSpec,
  SERVING,
  withReport,
} from '../../../testing/orchestrator/index.js';
import {agentNames} from '../names.js';
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
}

function stateOf(world: World): OrchestratorState {
  const scenario =
    world.live === false ? generateScenario({seed, subagents: 5, live: false}) : base;
  const withText =
    world.report === false
      ? scenario
      : withReport(scenario, {
          sequence: 2,
          kind: world.live === false ? ReportKind.FINAL : ReportKind.PROGRESS,
          atMs: REPORT_AT,
          headline: 'Throughput is 2.3 times the baseline at p99 118 ms.',
          changed: 'Fused expert dispatch landed.',
          openForUser: 'Should quantization stay paused?',
        });
  const options: MeasuredOptions = {
    seed,
    candidates: world.candidates ?? 40,
    startMs: START,
    endMs: END,
    objectives: world.objectives ?? SERVING,
    ...(world.revisedBound === undefined ? {} : {revisedBound: world.revisedBound}),
  };
  let state = withText.changes.reduce(applyChange, emptyState);
  state = applySearch(state, measuredSearch(RUN, options, {stale: world.stale === true}));
  const cost = world.cost === undefined ? COST : world.cost;
  return cost === null ? state : applyCost(state, cost);
}

function render(world: World, options: {freshness?: Freshness; costFeed?: CostFeedSnapshot} = {}) {
  const probe = {shown: [] as RunView[], stops: 0, runs: 0, catchUps: 0};
  const state = stateOf(world);
  const nowMs = world.live === false ? END + 60_000 : NOW;
  const report = runReport(state, RUN, nowMs);
  if (report === undefined) throw new Error('the fixture has its run');
  const screen = mount(
    <ReportScreen
      report={report}
      host="host-a"
      nowMs={nowMs}
      freshness={options.freshness ?? 'live'}
      staleForMs={options.freshness === undefined || options.freshness === 'live' ? null : 90_000}
      cost={state.costs[RUN]}
      costFeed={options.costFeed ?? {status: {_tag: 'Live'}, freshness: 'live'}}
      retryCost={() => undefined}
      stopProgress={undefined}
      stop={() => {
        probe.stops += 1;
      }}
      retryStop={() => undefined}
      catchUp={() => {
        probe.catchUps += 1;
      }}
      goRuns={() => {
        probe.runs += 1;
      }}
      openAgents={() => probe.shown.push('agents')}
      openCost={() => probe.shown.push('cost')}
      names={agentNames(state, RUN)}
      notice={null}
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

describe('the report as a page of the run', () => {
  it('opens on the trail, then the title, with Catch me up and Stop run in one group and no best line', () => {
    const {screen} = render({});
    const header = screen.container.querySelector('.vs-page-header');
    expect(header?.firstElementChild?.classList.contains('vs-page-trail')).toBe(true);
    expect(header?.querySelector('h1')?.textContent).toBe(stateOf({}).runs[RUN]?.task ?? 'a title');
    // What is best so far is the objective tiles', so the header does not say it a second time.
    expect(header?.textContent).not.toMatch(/\bBest\b/);
    // Both actions are the standard size, in the one group; the danger one is the outline, the
    // leading one is the filled button.
    const actions = [...(screen.container.querySelectorAll('.vs-page-actions .vs-button') ?? [])];
    expect(actions.map(button => button.textContent)).toEqual(['Catch me up', 'Stop run']);
    expect(actions.map(button => button.getAttribute('data-size'))).toEqual(['md', 'md']);
    expect(actions.map(button => button.getAttribute('data-variant'))).toEqual([
      'primary',
      'danger',
    ]);
    screen.unmount();
  });

  it('puts the objective tiles on the tracks the charts below them are on', () => {
    const {screen} = render({});
    const tiles = screen.container.querySelector('section.vs-tiles[aria-label="Objectives"]');
    expect(tiles?.getAttribute('data-track')).toBe('chart');
    expect(tiles?.querySelectorAll('article.vs-tile').length).toBeGreaterThan(1);
    expect(screen.container.querySelector('.vs-chart-columns')).not.toBeNull();
    screen.unmount();
  });

  it('calls the agents of a run that is over the agents it had, none of them working, with no rate of spend', () => {
    const live = render({});
    expect(cards(live.screen.container)).toContain('Live agents');
    expect(cardText(live.screen.container, 'Live agents')).toContain('working');
    expect(cardText(live.screen.container, 'Spend')).toContain('Last hour');
    live.screen.unmount();
    const over = render({live: false});
    expect(cards(over.screen.container)).not.toContain('Live agents');
    expect(cardText(over.screen.container, 'Agents')).not.toContain('working');
    expect(cardText(over.screen.container, 'Agents')).toContain('in all');
    expect(cardText(over.screen.container, 'Spend')).not.toContain('Last hour');
    expect(cardText(over.screen.container, 'Spend')).not.toContain('/h');
    over.screen.unmount();
  });

  it('says why a spend is a floor beside it, and gives the links of its cards one place and one look', () => {
    const {screen} = render({});
    expect(cardText(screen.container, 'Spend')).toMatch(/≥ \$318[\s\S]*41 unpriced/);
    const links = [...screen.container.querySelectorAll('.vs-card-head .vs-button')];
    expect(links.map(link => link.textContent)).toEqual(['Agents', 'Details']);
    // One style, in the heading's line of each card.
    expect(new Set(links.map(link => link.getAttribute('data-variant')))).toEqual(
      new Set(['quiet']),
    );
    expect(new Set(links.map(link => link.getAttribute('data-size')))).toEqual(new Set(['sm']));
    screen.unmount();
  });

  it('sets what the lead needs from the person apart, in the warning tone', () => {
    const {screen} = render({});
    const ask = [...screen.container.querySelectorAll('.vs-chip')].find(
      chip => chip.textContent === 'Needs you',
    );
    expect(ask?.getAttribute('data-tone')).toBe('warn');
    expect(ask?.parentElement?.textContent).toContain('Should quantization stay paused?');
    screen.unmount();
  });

  it('writes the age of the report and the time the run has run in the words of the Runs list', () => {
    const {screen} = render({});
    expect(cardText(screen.container, 'Lead summary')).toContain('Updated 23m ago');
    expect(screen.container.querySelector('.vs-page-meta')?.textContent).toMatch(
      /host-a(\d+d \d+h|\d+h \d+m|\d+m|\d+s)Started/,
    );
    expect(screen.text()).not.toMatch(/\d+ (s|min|h|d) ago/);
    screen.unmount();
  });
});

describe('the header of a report', () => {
  it('has Catch me up beside Stop while the run lives, and the press goes to the run', () => {
    const {screen, probe} = render({});
    screen.press(screen.control('Catch me up'));
    expect(probe.catchUps).toBe(1);
    expect(screen.controls('Stop run')).toHaveLength(1);
    screen.unmount();
  });

  it('keeps Catch me up when the run is over, which has no Stop', () => {
    const {screen} = render({live: false});
    expect(screen.controls('Catch me up')).toHaveLength(1);
    expect(screen.controls('Stop run')).toHaveLength(0);
    screen.unmount();
  });
});

describe('the report of a run with two objectives and a declared bound', () => {
  it('shows each section the data has, and none of an agent’s turns', () => {
    const {screen} = render({});
    expect(cards(screen.container)).toEqual([
      'Lead summary',
      'Trade-off',
      'Over time',
      'Live agents',
      'Spend',
      'By agent type',
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

  it('gives each objective a tile with the percent of the bound exactly as the host sent it', () => {
    const {screen} = render({});
    const sent = stateOf({}).searches[RUN]?.bounds ?? [];
    expect(sent).toHaveLength(2);
    const tiles = [...screen.container.querySelectorAll('.vs-tile')].map(tile => tile.textContent);
    for (const bound of sent) {
      const tile = tiles.find(text => text?.includes(bound.objective));
      expect(tile).toContain(`${Math.round(bound.percentOfBound ?? Number.NaN)}% of bound`);
    }
    screen.unmount();
  });

  it('starts every chart on the time axis at one left edge, whatever its labels are', () => {
    const wide: readonly ObjectiveSpec[] = [
      {name: 'small', direction: 1 as never, unit: 'x', base: 1, best: 9},
      {name: 'large', direction: 1 as never, unit: 'x', base: 1000, best: 98_000},
    ];
    const {screen} = render({objectives: wide});
    const lefts = [...screen.container.querySelectorAll('svg.vs-chart')]
      .filter(svg => svg.getAttribute('aria-label')?.includes('over time'))
      .map(svg => svg.querySelector('line.vs-chart-axis')?.getAttribute('x1'));
    expect(lefts).toHaveLength(2);
    expect(new Set(lefts).size).toBe(1);
    screen.unmount();
  });

  it('draws each measured candidate as a point and the best so far as a line, per objective', () => {
    const {screen} = render({candidates: 25});
    const charts = [...screen.container.querySelectorAll('.vs-chart-panel')].filter(panel =>
      panel.querySelector('svg.vs-chart')?.getAttribute('aria-label')?.includes('over time'),
    );
    expect(charts).toHaveLength(2);
    for (const chart of charts) {
      expect(chart.querySelectorAll('circle.vs-chart-point')).toHaveLength(25);
      expect(chart.querySelectorAll('path.vs-chart-step')).toHaveLength(1);
      const label = chart.querySelector('.vs-chart-reference-label');
      expect(label?.getAttribute('aria-description')).toContain('Derived from');
      expect(chart.querySelectorAll('.vs-chart-reference')).toHaveLength(1);
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

  it('puts the lead’s latest report in the summary with its age', () => {
    const {screen} = render({});
    const summary = screen.container.querySelector('section.vs-card')?.textContent ?? '';
    expect(summary).toContain('Throughput is 2.3 times the baseline');
    expect(summary).toContain('Updated 23m ago');
    expect(summary).toContain('Fused expert dispatch landed.');
    expect(summary).toContain('Needs you');
    screen.unmount();
  });

  it('shows the cost as the host sent it: spend against its cap, the last hour, and the split by type', () => {
    const {screen} = render({});
    const spend = cardText(screen.container, 'Spend');
    expect(spend).toContain('$318');
    expect(spend).toContain('of $500');
    expect(spend).toContain('Last hour');
    expect(spend).toContain('1,433');
    expect(cardText(screen.container, 'By agent type')).toContain('Worker');
    screen.unmount();
  });

  it('counts the agents by type', () => {
    const {screen} = render({});
    const agents = [...screen.container.querySelectorAll('section.vs-card')].find(card =>
      card.textContent?.startsWith('Live agents'),
    );
    expect(agents?.textContent).toMatch(/Worker\d+of \d+/);
    expect(agents?.textContent).toContain('in all');
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
    expect(screen.container.querySelectorAll('.vs-chart-panel')).toHaveLength(1);
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

  it('says there is nothing measured yet, and draws no tile, chart or trade-off', () => {
    const {screen} = render({candidates: 0, objectives: SERVING.map(o => ({...o}))});
    const text = screen.text();
    expect(text).toContain('No measurements yet');
    expect(cards(screen.container)).not.toContain('Trade-off');
    expect(screen.container.querySelectorAll('svg.vs-chart')).toHaveLength(0);
    screen.unmount();
  });

  it('shows no bound and no share of one when none was declared', () => {
    const {screen} = render({objectives: SERVING.map(({bound: _, ...o}) => o)});
    expect(screen.container.querySelectorAll('.vs-chart-reference-label')).toHaveLength(0);
    expect(screen.container.querySelectorAll('.vs-chart-reference')).toHaveLength(0);
    expect(screen.text()).not.toContain('% of bound');
    // The tiles still show what the head measured.
    expect(screen.container.querySelectorAll('.vs-tile')).toHaveLength(2);
    screen.unmount();
  });

  it('says only that there is no report when the lead has posted none', () => {
    const {screen} = render({report: false});
    expect(screen.container.querySelector('section.vs-card')?.textContent).toContain(
      'No report yet',
    );
    screen.unmount();
  });

  it('for an ended run has no Stop, counts the time to its end, and calls a final report final', () => {
    const {screen} = render({live: false});
    expect(screen.controls('Stop run')).toHaveLength(0);
    expect(screen.text()).toContain('Finished');
    expect(screen.container.querySelector('section.vs-card')?.textContent).toContain('Final');
    screen.unmount();
  });

  it('shows a dash for a cost no turn priced, and says the others are floors', () => {
    const none = render({cost: NO_PRICE});
    expect(cardText(none.screen.container, 'Spend')).toContain('\u2014');
    none.screen.unmount();
    const floor = render({});
    expect(cardText(floor.screen.container, 'Spend')).toContain('\u2265');
    floor.screen.unmount();
  });

  it('shows no cap and no meter for a run with no cost limit', () => {
    const {screen} = render({cost: {...COST, maxCostUsd: null}});
    const spend = [...screen.container.querySelectorAll('section.vs-card')].find(card =>
      card.textContent?.startsWith('Spend'),
    );
    expect(spend?.textContent).not.toContain(' of $');
    expect(spend?.querySelectorAll('.vs-meter')).toHaveLength(0);
    screen.unmount();
  });

  it('says the cost is being read until the host has sent it, and why it failed when it did', () => {
    const reading = render({cost: null});
    expect(cardText(reading.screen.container, 'Cost')).toContain('Reading cost');
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
    expect(cardText(failed.screen.container, 'Cost')).toContain('cannot be reached');
    expect(failed.screen.controls('Retry cost')).toHaveLength(1);
    failed.screen.unmount();
  });
});

describe('what the header says about freshness', () => {
  it('says nothing while the data is current, and says when it is not', () => {
    const live = render({});
    expect(live.screen.text()).not.toMatch(/Stale|Catching up|Unavailable|Connecting/);
    live.screen.unmount();
    fc.assert(
      fc.property(
        fc.constantFrom('catching-up', 'stale', 'empty', 'unavailable') as fc.Arbitrary<Freshness>,
        freshness => {
          const {screen} = render({}, {freshness});
          expect(screen.text()).toMatch(/Stale|Catching up|Unavailable|Connecting/);
          screen.unmount();
        },
      ),
      {numRuns: 8},
    );
  });

  it('says how long the data has been stale, in the chip\u2019s hint', () => {
    const {screen} = render({}, {freshness: 'stale'});
    const chip = [...screen.container.querySelectorAll('.vs-chip')].find(
      each => each.textContent === 'Stale',
    );
    expect(chip?.getAttribute('aria-description')).toContain('Last current 1m ago');
    screen.unmount();
  });

  it('says the search state is stale in the header, not as a banner', () => {
    const {screen} = render({stale: true});
    expect(screen.text()).toContain('Search stale');
    expect(screen.container.querySelector('[role="alert"], .vs-banner')).toBeNull();
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

  it('renders, names nothing from the protocol, and shows one chart per measured objective', () => {
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
        const charts = [...screen.container.querySelectorAll('svg.vs-chart')].filter(svg =>
          svg.getAttribute('aria-label')?.includes('over time'),
        );
        expect(charts).toHaveLength(data.candidates === 0 ? 0 : data.objectives);
        screen.unmount();
      }),
      {numRuns: 25, seed: 4050},
    );
  });
});
