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
  ReceiptSchema,
  ReportKind,
  SearchStateSchema,
} from '@vibesys/orchestrator-proto';
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
  type Freshness,
  type IdeaWindow,
  type OrchestratorState,
  objectiveReports,
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
  serveSummaries,
  withReport,
} from '../../../testing/orchestrator/index.js';
import {agentNames} from '../names.js';
import {formatExact, formatFigure, withUnit} from './format.js';
import {ObjectiveTiles} from './objective-tiles.js';
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
    ...(world.ideas === undefined ? {} : {ideas: world.ideas}),
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
    freshness?: Freshness;
    costFeed?: CostFeedSnapshot;
    stopProgress?: CommandProgress;
  } = {},
) {
  const probe = {shown: [] as RunView[], stops: 0, runs: 0, catchUps: 0};
  const state = stateOf(world);
  const nowMs = world.live === false ? END + 60_000 : NOW;
  const report = runReport(state, RUN, nowMs);
  if (report === undefined) throw new Error('the fixture has its run');
  const screen = mount(
    <ReportScreen
      report={report}
      host="host-a"
      ideasWindow={world.window ?? null}
      nowMs={nowMs}
      freshness={options.freshness ?? 'live'}
      staleForMs={options.freshness === undefined || options.freshness === 'live' ? null : 90_000}
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
    expect(links.map(link => link.textContent)).toEqual(['Details', 'Agents']);
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
  it('has Catch me up beside Stop while the run lives, and the press opens its page', () => {
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
      'Workstreams',
      'Spend',
      'By agent type',
      'Ideas',
      'Live agents',
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

  it('gives each objective a tile with the best’s percent of the bound exactly as the host sent it', () => {
    const {screen} = render({});
    const sent = stateOf({}).searches[RUN]?.bounds ?? [];
    expect(sent).toHaveLength(2);
    const tiles = [...screen.container.querySelectorAll('.vs-tile')].map(tile => tile.textContent);
    for (const bound of sent) {
      const tile = tiles.find(text => text?.includes(bound.objective));
      expect(tile).toContain(`${Math.round(bound.bestPercentOfBound ?? Number.NaN)}% of bound`);
    }
    screen.unmount();
  });

  it('shows one number for an objective’s best in its tile and its chart, whatever the head scored', () => {
    const {screen} = render({});
    const search = stateOf({}).searches[RUN];
    const objectives = search?.series ?? [];
    expect(objectives).toHaveLength(2);
    let headDiffers = 0;
    for (const series of objectives) {
      const bound = search?.bounds.find(entry => entry.objective === series.objective);
      const unit = bound?.versions.at(-1)?.unit ?? null;
      const best = series.points.filter(point => point.bestSoFar).at(-1)?.value ?? Number.NaN;
      const figure = formatFigure(best);
      // The tile and the chart panel say it in their text.
      const tile = [...screen.container.querySelectorAll('.vs-tile')].find(each =>
        each.textContent?.includes(series.objective),
      );
      expect(tile?.textContent).toContain(figure);
      const panel = [...screen.container.querySelectorAll('.vs-chart-panel')].find(each =>
        each.textContent?.includes(series.objective),
      );
      expect(panel?.textContent).toContain(figure);
      // The head is the best of the first objective only: on the other it scored something else, and
      // that is not what any of the three says.
      const head = bound?.headValue;
      if (head !== undefined && formatFigure(head) !== figure) {
        headDiffers += 1;
        expect(tile?.textContent).not.toContain(formatFigure(head));
        expect(
          screen.container.querySelector(`[title="${withUnit(formatExact(head), unit)}"]`),
        ).toBeNull();
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

  it('shows the outcome of a Stop while the run lives and drops it once the run is over', () => {
    const accepted: CommandProgress = {_tag: 'Accepted', receipt: create(ReceiptSchema, {})};
    const live = render({}, {stopProgress: accepted});
    expect(live.screen.text()).toContain('Stop: accepted');
    live.screen.unmount();
    const over = render({live: false}, {stopProgress: accepted});
    expect(over.screen.text()).not.toContain('Stop: accepted');
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
      expect(over.screen.text()).not.toContain('Stop:');
      over.screen.unmount();
    }
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
    expect(cardText(failed.screen.container, 'Cost')).toContain('Unreachable');
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

describe('the ideas and workstreams of the report', () => {
  const table = (root: HTMLElement, name: string): HTMLTableElement | null =>
    root.querySelector(`table[aria-label="${name}"]`);
  const rowsOf = (root: HTMLElement, name: string): string[][] =>
    [...(table(root, name)?.querySelectorAll('tbody tr') ?? [])].map(row =>
      [...row.querySelectorAll('td')].map(cell => cell.textContent ?? ''),
    );

  /** Chooses the segment with this label, as a person does. */
  const choose = (root: HTMLElement, label: string): void => {
    const segment = [...root.querySelectorAll('label.vs-segment')].find(
      each => each.textContent === label,
    );
    const input = segment?.querySelector<HTMLInputElement>('input');
    if (input === null || input === undefined) throw new Error(`no segment ${label}`);
    act(() => input.click());
  };

  it('shows the ideas the run worked on by default, each with its served verdict, gain and candidate count', () => {
    const {screen} = render({ideas: 12});
    const served = stateOf({ideas: 12}).searches[RUN];
    const inRun = (served?.ideas ?? []).filter(view => view.inRun);
    expect(inRun.length).toBeGreaterThan(0);
    expect(inRun.length).toBeLessThan(12);
    const rows = rowsOf(screen.container, 'Idea list');
    expect(rows).toHaveLength(inRun.length);
    for (const view of inRun) {
      const row = rows.find(cells => cells[0] === `#${view.planNumber}`);
      expect(row?.[5]).toBe(String(view.candidateJobIds.length));
      const gain = view.best[0];
      expect(row?.[4]).toBe(gain === undefined ? '—' : `${gain.ratio.toFixed(2)}×`);
    }
    // Newest first.
    const numbers = rows.map(cells => Number(cells[0]?.slice(1)));
    expect(numbers).toEqual([...numbers].sort((a, b) => b - a));
    screen.unmount();
  });

  it('cuts a long title to its column and says it whole on hover', () => {
    const {screen} = render({ideas: 12});
    choose(screen.container, 'All 12');
    const long = [...screen.container.querySelectorAll('td[data-clip]')].find(cell =>
      cell.getAttribute('title')?.startsWith('A speculative draft reuse'),
    );
    expect(long?.getAttribute('title')?.length).toBeGreaterThan(60);
    expect(long?.textContent).toBe(long?.getAttribute('title'));
    screen.unmount();
  });

  it('shows the workstreams with the host’s counts and best gain, and no count of its own', () => {
    const {screen} = render({ideas: 12});
    const served = stateOf({ideas: 12}).searches[RUN];
    const rows = rowsOf(screen.container, 'Workstream list');
    expect(rows).toHaveLength(served?.workstreamViews.length ?? -1);
    for (const view of served?.workstreamViews ?? []) {
      const total = view.kept + view.rejected + view.open + view.parked;
      expect(rows.some(cells => cells[2] === String(total))).toBe(true);
    }
    expect(cardText(screen.container, 'Workstreams')).toContain('No workstream');
    screen.unmount();
  });

  it('says how many candidates name no plan, and marks a state the host cut short, in a label and not a banner', () => {
    const {screen} = render({ideas: 12, truncated: true});
    const served = stateOf({ideas: 12}).searches[RUN];
    expect(cardText(screen.container, 'Ideas')).toContain(`${served?.unplanned} unplanned`);
    expect(cardText(screen.container, 'Ideas')).toContain('Some left out');
    expect(screen.container.querySelector('[role="alert"]')).toBeNull();
    screen.unmount();
    const whole = render({ideas: 12});
    expect(cardText(whole.screen.container, 'Ideas')).not.toContain('Some left out');
    whole.screen.unmount();
  });

  it('has an empty state for a run with no ideas', () => {
    const {screen} = render({ideas: 0});
    expect(cardText(screen.container, 'Ideas')).toContain('No ideas');
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
    choose(screen.container, 'All 12');
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

describe('the objective tiles and the bound’s unit', () => {
  const search = create(SearchStateSchema, {
    series: [
      {
        objective: 'tok_s',
        direction: Direction.MAXIMIZE,
        points: [{jobId: 'j', commit: 'c', at: timestampFromMs(1000), value: 5, bestSoFar: true}],
      },
    ],
  });
  const tileText = (check: BoundUnitCheck): string => {
    const standing = create(ObjectiveStandingSchema, {
      name: 'tok_s',
      bestValue: 5,
      unit: 'tok/s',
      boundUnit: 'ms',
      boundUnitCheck: check,
      bestPercentOfBound: 40,
    });
    const screen = mount(<ObjectiveTiles objectives={objectiveReports(search, [standing])} />);
    const text = screen.text();
    screen.unmount();
    return text;
  };

  it('names both units in a short label when the bound is in another unit, and shows the measured unit beside the best', () => {
    const text = tileText(BoundUnitCheck.MISMATCH);
    expect(text).toContain('tok/s vs bound ms');
    expect(text).toContain('tok/s');
    expect(text).not.toContain('% of bound');
  });

  it('shows the share of the bound as verified, unverified or not at all as the host checked it', () => {
    expect(tileText(BoundUnitCheck.MATCH)).toContain('40% of bound');
    expect(tileText(BoundUnitCheck.MATCH)).not.toContain('unverified');
    expect(tileText(BoundUnitCheck.UNKNOWN)).toContain('40% of bound · unverified');
  });
});
