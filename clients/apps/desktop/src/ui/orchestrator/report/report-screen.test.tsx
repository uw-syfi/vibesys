/**
 * The run report through the DOM, over served views: the search view with its series, trade-off and
 * bounds, the lead's reports and the cost view, as the host would send them. Nothing here works out a
 * best, a frontier or a cost; each test says what the host sent and reads what a person sees.
 */
import {describe, expect, it} from '@effect/vitest';
import {AgentRole, oneofIs, ReportKind} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  applyChange,
  applyCost,
  applySearch,
  emptyState,
  type FeedStatus,
  type Freshness,
  type OrchestratorState,
  type RunView,
} from '../../../state/index.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {
  type CostSpec,
  costFixture,
  generateScenario,
  type MeasuredOptions,
  measuredSearch,
  type ObjectiveSpec,
  SERVING,
  withReport,
} from '../../../testing/orchestrator/index.js';
import {RunScreen} from '../run-screen.js';

installDom();

const seed = 4;
const base = generateScenario({seed, subagents: 5, live: true});
const RUN = base.runId;
const START = Date.UTC(2026, 10, 1, 17, 0, 0);
const END = base.endMs;
const REPORT_AT = END;
const NOW = END + 23 * 60_000;

const TYPES: CostSpec['types'] = [
  {role: AgentRole.WORKER, agents: 4, costUsd: 205},
  {role: AgentRole.LEAD, agents: 1, costUsd: 66},
  {role: AgentRole.PLANNER, agents: 2, costUsd: 37},
  {role: AgentRole.PROFILER, agents: 1, costUsd: null},
];
const COST: CostSpec = {
  asOfMs: NOW,
  startMs: START,
  totalUsd: 318,
  limitUsd: 500,
  lastHourUsd: 12.4,
  types: TYPES,
};

interface World {
  readonly objectives?: readonly ObjectiveSpec[];
  readonly candidates?: number;
  readonly report?: boolean;
  readonly cost?: CostSpec | null;
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
  return cost === null ? state : applyCost(state, costFixture(RUN, cost));
}

function render(
  world: World,
  options: {freshness?: Freshness; costFeed?: FeedStatus; view?: RunView} = {},
) {
  const probe = {shown: [] as RunView[], stops: 0, runs: 0};
  const screen = mount(
    <RunScreen
      state={stateOf(world)}
      host="host-a"
      runId={RUN}
      nowMs={world.live === false ? END + 60_000 : NOW}
      freshness={options.freshness ?? 'live'}
      costFeed={options.costFeed ?? {_tag: 'Live'}}
      view={options.view ?? 'report'}
      agentId={null}
      agentPanel={null}
      stopProgress={undefined}
      stop={() => {
        probe.stops += 1;
      }}
      retryStop={() => undefined}
      goRuns={() => {
        probe.runs += 1;
      }}
      show={view => probe.shown.push(view)}
      pick={() => undefined}
    />,
  );
  return {screen, probe};
}

const cards = (root: HTMLElement): string[] =>
  [...root.querySelectorAll('section.vs-card > h2')].map(heading => heading.textContent ?? '');

describe('the report of a run with two objectives and a declared bound', () => {
  it('shows each section the data has, and none of an agent’s turns', () => {
    const {screen} = render({});
    expect(cards(screen.container)).toEqual([
      'Lead summary',
      'Trade-off',
      'Over time',
      'Live agents',
      'Cost',
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
    expect(summary).toContain('Updated 23 min ago');
    expect(summary).toContain('Fused expert dispatch landed.');
    expect(summary).toContain('Needs you');
    screen.unmount();
  });

  it('shows the cost as the host sent it: total and limit, the last hour, and the split by type', () => {
    const {screen} = render({});
    const cost = [...screen.container.querySelectorAll('section.vs-card')].find(card =>
      card.textContent?.startsWith('Cost'),
    );
    const text = cost?.textContent ?? '';
    expect(text).toContain('$318');
    expect(text).toContain('of $500');
    expect(text).toContain('$12.4');
    expect(text).toContain('Worker ×4');
    expect(text).toContain('$205');
    // The unpriced type has no share and no price.
    expect(text).not.toContain('Profiler');
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

  it('shows a dash and says partial for a cost with no price, and a split with nothing priced has no bar', () => {
    const {screen} = render({
      cost: {
        ...COST,
        totalUsd: null,
        lastHourUsd: null,
        unpricedTurns: 3,
        types: TYPES.map(type => ({...type, costUsd: null})),
      },
    });
    const cost = [...screen.container.querySelectorAll('section.vs-card')].find(card =>
      card.textContent?.startsWith('Cost'),
    );
    expect(cost?.textContent).toContain('—');
    expect(cost?.textContent).toContain('Partial');
    expect(cost?.querySelector('.vs-share')).toBeNull();
    screen.unmount();
  });

  it('shows no limit and no meter for a run with no cost limit', () => {
    const {screen} = render({cost: {...COST, limitUsd: null}});
    const cost = [...screen.container.querySelectorAll('section.vs-card')].find(card =>
      card.textContent?.startsWith('Cost'),
    );
    expect(cost?.textContent).not.toContain('of $');
    expect(cost?.querySelectorAll('.vs-meter')).toHaveLength(0);
    screen.unmount();
  });

  it('says the cost is being read until the host has sent it, and that it failed when it did', () => {
    const reading = render({cost: null});
    const cost = (root: HTMLElement) =>
      [...root.querySelectorAll('section.vs-card')].find(card =>
        card.textContent?.startsWith('Cost'),
      )?.textContent;
    expect(cost(reading.screen.container)).toContain('Reading cost');
    reading.screen.unmount();
    const failed = render(
      {cost: null},
      {costFeed: {_tag: 'Failed', fault: {kind: 'Unavailable', detail: '', retryable: true}}},
    );
    expect(cost(failed.screen.container)).toContain('Cost unavailable');
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

  it('says the search state is stale inline, not as a banner', () => {
    const {screen} = render({stale: true});
    expect(screen.text()).toContain('Search state is stale');
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
          cost: data.priced ? COST : {...COST, totalUsd: null, lastHourUsd: null},
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
