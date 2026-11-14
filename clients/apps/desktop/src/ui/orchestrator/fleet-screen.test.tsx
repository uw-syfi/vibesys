import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  BoundUnitCheck,
  Direction,
  Harness,
  IdeaVerdict,
  IdeaViewSchema,
  MessageSchema,
  PlanSchema,
  PlanStatus,
  ReportKind,
  ReportSchema,
  type Run,
  RunSchema,
  RunSearchResultSchema,
  RunStatus,
  RunSummarySchema,
  RunsSnapshotSchema,
  SearchRunsResponseSchema,
  type SearchState,
  SearchStateSchema,
  TaskCardSchema,
} from '@vibesys/orchestrator-proto';
import {act, type JSX, useReducer} from 'react';
import type {RunSearchState, RunsRefreshState} from '../../core/index.js';
import {
  applySnapshot,
  emptyState,
  type FleetHostInput,
  type Freshness,
  type GenericRunEntry,
  type GenericRunIndex,
  type HostStatus,
  type HostStatusKind,
  initialNavigation,
  type NavigationAction,
  navigationReducer,
  type OrchestratorState,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {measuredSearch, SERVING, serveSummaries} from '../../testing/orchestrator/index.js';
import {DisplayClockProvider} from '../display-clock.js';
import {TEST_ZONE} from '../test-display-clock.js';
import {FleetScreen} from './fleet-screen.js';

installDom();

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const NOW = Date.UTC(2026, 10, 7, 10, 0, 0);
const EMPTY_RUN_INDEX: GenericRunIndex = {
  _tag: 'Ready',
  freshness: 'current',
  runs: new Map(),
};

interface RunFacts {
  readonly id: string;
  readonly task: string;
  readonly status: RunStatus;
  readonly startedMs: number;
  readonly endedMs?: number;
  readonly repository?: string;
  readonly cost?: number;
  readonly budget?: number;
  readonly reportMs?: number;
  readonly reportHeadline?: string;
  readonly working?: number;
  readonly result?: {readonly value: number; readonly unit: string; readonly percent?: number};
  readonly search?: SearchState;
  readonly card?: MessageInitShape<typeof TaskCardSchema>;
  readonly allowedAgents?: readonly {readonly harness: Harness; readonly model: string}[];
}

const runOf = (run: RunFacts): Run =>
  create(RunSchema, {
    id: run.id,
    task: run.task,
    repository: run.repository ?? 'decode-service',
    status: run.status,
    createdAt: timestampFromMs(run.startedMs),
    ...(run.endedMs === undefined ? {} : {endedAt: timestampFromMs(run.endedMs)}),
    ...(run.cost === undefined ? {} : {usage: {costUsd: run.cost}}),
    ...(run.budget === undefined ? {} : {maxCostUsd: run.budget}),
    ...(run.card === undefined ? {} : {taskCard: create(TaskCardSchema, run.card)}),
    allowedAgents: [...(run.allowedAgents ?? [])],
  });

function stateOf(runs: readonly RunFacts[]): OrchestratorState {
  const snapshot = create(RunsSnapshotSchema, {
    runs: runs.map(runOf),
    agents: runs.flatMap(run =>
      Array.from({length: run.working ?? 0}, (_, index) =>
        create(AgentSchema, {
          id: `${run.id}-a${index}`,
          runId: run.id,
          role: AgentRole.WORKER,
          harness: Harness.CODEX,
          status: AgentStatus.ACTIVE,
        }),
      ),
    ),
    messages: runs.flatMap(run =>
      run.reportMs === undefined
        ? []
        : [
            create(MessageSchema, {
              id: `${run.id}-report`,
              runId: run.id,
              at: timestampFromMs(run.reportMs),
              body: {
                case: 'report',
                value: create(ReportSchema, {
                  headline: run.reportHeadline ?? 'Progress report',
                  kind: ReportKind.PROGRESS,
                  sequence: 2,
                }),
              },
            }),
          ],
    ),
    searches: runs.flatMap(run =>
      run.search !== undefined
        ? [run.search]
        : run.result === undefined
          ? []
          : [
              create(SearchStateSchema, {
                runId: run.id,
                revision: 1n,
                head: {
                  state: {
                    case: 'current',
                    value: {
                      head: 'b'.repeat(40),
                      result: {
                        outcome: {
                          case: 'measure',
                          value: {metric: {name: 'throughput'}, value: run.result.value},
                        },
                      },
                    },
                  },
                },
                bounds: [
                  {
                    objective: 'throughput',
                    direction: Direction.MAXIMIZE,
                    versions: [
                      {
                        seq: 1n,
                        objective: 'throughput',
                        direction: Direction.MAXIMIZE,
                        value: 1000,
                        unit: run.result.unit,
                        origin: {runId: run.id, agent: 'lead', at: timestampFromMs(NOW)},
                      },
                    ],
                    headValue: run.result.value,
                    bestValue: run.result.value,
                    measuredUnit: run.result.unit,
                    unitCheck: BoundUnitCheck.MATCH,
                    ...(run.result.percent === undefined
                      ? {}
                      : {
                          percentOfBound: run.result.percent,
                          bestPercentOfBound: run.result.percent,
                        }),
                  },
                ],
              }),
            ],
    ),
    sequence: 1n,
  });
  return serveSummaries(applySnapshot(emptyState, snapshot));
}

const statusFor = (
  freshness: Freshness,
  staleForMs: number | null,
  refused = false,
): HostStatus => {
  if (refused) return {kind: 'refused', cause: 'method-not-available', staleForMs: null};
  const kinds: Record<Freshness, HostStatusKind> = {
    live: 'live',
    'catching-up': 'catching-up',
    stale: 'stale',
    empty: 'waiting',
    unavailable: 'unavailable',
  };
  const kind = kinds[freshness];
  return {kind, cause: null, staleForMs: kind === 'stale' ? staleForMs : null};
};

const hypothesisSearch = (): SearchState =>
  create(SearchStateSchema, {
    runId: 'live-1',
    revision: 3n,
    plans: [
      create(PlanSchema, {
        number: 7,
        title: 'Batch consumer wake-ups',
        body: 'Reduce lock hand-offs on the hot path.',
        status: PlanStatus.OPEN,
        updatedAt: timestampFromMs(NOW - 2 * MINUTE),
      }),
    ],
    ideas: [
      create(IdeaViewSchema, {
        planNumber: 7,
        verdict: IdeaVerdict.OPEN,
        inRun: true,
      }),
    ],
  });

const liveRun: RunFacts = {
  id: 'live-1',
  task: 'Queue throughput',
  status: RunStatus.RUNNING,
  startedMs: NOW - 5 * HOUR - 20 * MINUTE,
  cost: 318,
  budget: 500,
  reportMs: NOW - 23 * MINUTE,
  working: 3,
  search: hypothesisSearch(),
  card: {
    task: 'queue-rs',
    modelName: 'Queue model',
    workloadDescription: 'Make the bounded MPMC queue faster without breaking ordering.',
  },
  allowedAgents: [{harness: Harness.CODEX, model: 'gpt-5.3-codex'}],
};

const endedRun: RunFacts = {
  id: 'ended-1',
  task: 'Tokenizer throughput',
  status: RunStatus.ENDED,
  startedMs: NOW - 30 * HOUR,
  endedMs: NOW - 24 * HOUR,
  cost: 12.4,
  result: {value: 2310, unit: 'tok/s', percent: 71.2},
};

const hostA: FleetHostInput = {
  key: 'local',
  label: 'This Mac',
  freshness: 'live',
  status: statusFor('live', null),
  data: stateOf([liveRun, endedRun]),
  runIndex: EMPTY_RUN_INDEX,
};

const hostB: FleetHostInput = {
  key: 'ssh:gpu-cluster-1',
  label: 'gpu-cluster-1',
  freshness: 'stale',
  status: statusFor('stale', 14 * MINUTE),
  runIndex: EMPTY_RUN_INDEX,
  data: stateOf([
    {
      id: 'remote-1',
      task: 'KV cache kernel',
      status: RunStatus.RUNNING,
      startedMs: NOW - HOUR,
      cost: 1204.5,
    },
  ]),
};

interface ScreenProps {
  readonly hosts: readonly FleetHostInput[];
  readonly log: NavigationAction[];
  readonly refreshed: string[];
  readonly refreshStates?: Readonly<Record<string, RunsRefreshState>>;
  readonly searchStates?: Readonly<Record<string, RunSearchState>>;
  readonly newRun?: JSX.Element | null;
}

function Screen(props: ScreenProps): JSX.Element {
  const [navigation, move] = useReducer(navigationReducer, initialNavigation);
  return (
    <DisplayClockProvider zone={TEST_ZONE}>
      <FleetScreen
        hosts={props.hosts}
        place={navigation.fleet}
        nowMs={NOW}
        go={action => {
          props.log.push(action);
          move(action);
        }}
        refreshStates={
          props.refreshStates ??
          Object.fromEntries(props.hosts.map(host => [host.key, {_tag: 'Idle'} as const]))
        }
        refreshHost={host => props.refreshed.push(host)}
        searchStates={
          props.searchStates ??
          Object.fromEntries(props.hosts.map(host => [host.key, {_tag: 'Idle'} as const]))
        }
        newRun={props.newRun ?? <button type="button">New run</button>}
      />
    </DisplayClockProvider>
  );
}

const open = (
  hosts: readonly FleetHostInput[] = [hostA, hostB],
  refreshStates?: Readonly<Record<string, RunsRefreshState>>,
  searchStates?: Readonly<Record<string, RunSearchState>>,
) => {
  const log: NavigationAction[] = [];
  const refreshed: string[] = [];
  const render = (states = refreshStates, searches = searchStates): JSX.Element => (
    <Screen
      hosts={hosts}
      log={log}
      refreshed={refreshed}
      {...(states === undefined ? {} : {refreshStates: states})}
      {...(searches === undefined ? {} : {searchStates: searches})}
    />
  );
  const screen = mount(render());
  return {log, refreshed, render, screen};
};

const cards = (screen: ReturnType<typeof mount>): HTMLElement[] => [
  ...screen.container.querySelectorAll<HTMLElement>('article.vs-summary-card'),
];

function cardOf(screen: ReturnType<typeof mount>, title: string): HTMLElement {
  const found = cards(screen).find(card =>
    card.querySelector('.vs-summary-card-title')?.textContent?.includes(title),
  );
  if (found === undefined) throw new Error(`no run card named ${title}`);
  return found;
}

function changeSearch(screen: ReturnType<typeof mount>, value: string): void {
  const input = screen.container.querySelector<HTMLInputElement>('input[type="search"]');
  if (input === null) throw new Error('no runs search');
  const View = input.ownerDocument.defaultView as unknown as {Event: typeof Event};
  const setter = Object.getOwnPropertyDescriptor(
    Object.getPrototypeOf(input) as object,
    'value',
  )?.set;
  if (setter === undefined) throw new Error('the DOM has no input value setter');
  act(() => {
    input.focus();
    setter.call(input, value);
    for (const name of ['input', 'change', 'keyup'])
      input.dispatchEvent(new View.Event(name, {bubbles: true}));
  });
}

describe('the Runs operator page', () => {
  it('shows live and ended runs as full-width summaries with the operator facts', () => {
    const {screen} = open();
    expect(cards(screen)).toHaveLength(3);
    const live = screen.container.querySelector('section[aria-label="Live runs"]');
    const ended = screen.container.querySelector('section[aria-label="Ended runs"]');
    expect(live?.querySelectorAll('article')).toHaveLength(2);
    expect(ended?.querySelectorAll('article')).toHaveLength(1);
    expect(live?.compareDocumentPosition(ended as Node)).toBe(4);

    const queue = cardOf(screen, 'Queue model');
    expect(queue.textContent).toContain(
      'Make the bounded MPMC queue faster without breaking ordering.',
    );
    expect(queue.textContent).toContain('Batch consumer wake-ups');
    expect(queue.textContent).toContain('Latest hypothesis');
    expect(queue.textContent).toContain('This Mac');
    expect(queue.textContent).toContain('Agents3 working');
    expect(queue.textContent).toContain('Spend$318of $500');
    expect(queue.textContent).toContain('Running 5h 20m');

    const endedCard = cardOf(screen, 'Tokenizer throughput');
    expect(endedCard.textContent).toContain('Best so far2.31k tok/s');
    expect(endedCard.textContent).toContain('71% of bound');
    expect(endedCard.textContent).toContain('$12.40');
    expect(endedCard.textContent).toContain('Finished');
    screen.unmount();
  });

  it('keeps New run and semantic search in the fixed header', () => {
    const {screen} = open();
    expect(screen.controls('New run')).toHaveLength(1);
    const input = screen.container.querySelector('input[type="search"]');
    expect(input?.getAttribute('aria-label')).toBe('Search runs');
    expect(input?.getAttribute('placeholder')).toContain('model');
    expect(input?.closest('.vs-page-header')).not.toBeNull();
    screen.unmount();
  });

  it('filters by title, task model, CLI model, description, and structured work', () => {
    const {screen, log} = open();
    for (const query of [
      'queue',
      'Queue model',
      'gpt-5.3-codex',
      'bounded ordering',
      'consumer wake-ups',
    ]) {
      changeSearch(screen, query);
      expect(log.at(-1)).toEqual({type: 'filter-runs-by-query', query});
      expect(cards(screen)).toHaveLength(1);
      expect(cardOf(screen, 'Queue model')).not.toBeNull();
    }
    changeSearch(screen, 'something impossible');
    expect(cards(screen)).toHaveLength(0);
    expect(screen.text()).toContain('No matching');
    screen.unmount();
  });

  it('combines All, Live, Ended, and host filters without changing their intents', () => {
    const {screen, log} = open();
    const radio = (value: string): HTMLElement => {
      const found = screen.container.querySelector<HTMLElement>(
        `input[type="radio"][value="${value}"]`,
      );
      if (found === null) throw new Error(`no ${value} filter`);
      return found;
    };
    screen.press(radio('ended'));
    expect(log.at(-1)).toEqual({type: 'filter-runs-by-state', state: 'ended'});
    expect(cards(screen)).toHaveLength(1);
    expect(cardOf(screen, 'Tokenizer')).not.toBeNull();
    screen.press(radio('all'));

    const select = screen.container.querySelector<HTMLSelectElement>('select');
    if (select === null) throw new Error('no host filter');
    const View = select.ownerDocument.defaultView as unknown as {Event: typeof Event};
    act(() => {
      select.value = hostB.key;
      select.dispatchEvent(new View.Event('change', {bubbles: true}));
    });
    expect(log.at(-1)).toEqual({type: 'filter-runs-by-host', host: hostB.key});
    expect(cards(screen)).toHaveLength(1);
    expect(cardOf(screen, 'KV cache kernel')).not.toBeNull();
    screen.unmount();
  });

  it('shows and opens generic and unknown runs without inventing managed details', () => {
    const generic: GenericRunEntry = {
      runId: 'generic-live',
      projectRoot: '/work/operator-console',
      state: 'running',
      activity: 'live',
      objective: {name: 'latency', unit: 'ms', bestValue: 12.5},
      costUsd: 4.25,
      needsUser: true,
    };
    const unknown: GenericRunEntry = {
      ...generic,
      runId: 'generic-unknown',
      projectRoot: null,
      state: 'unknown',
      activity: 'unknown',
      objective: null,
      costUsd: null,
      needsUser: false,
    };
    const host: FleetHostInput = {
      ...hostA,
      data: emptyState,
      runIndex: {
        _tag: 'Ready',
        freshness: 'current',
        runs: new Map([
          [generic.runId, generic],
          [unknown.runId, unknown],
        ]),
      },
    };
    const {screen, log} = open([host]);

    const live = cardOf(screen, 'operator-console');
    expect(live.textContent).toContain('Running');
    expect(live.textContent).toContain('Operator action');
    expect(live.textContent).toContain('headline result, not the measurement series');
    // Nothing the host index does not carry is drawn: no agents, no workstreams, no time left.
    expect(live.textContent).not.toContain('Agents');
    expect(live.textContent).not.toContain('Workstreams');
    expect(
      screen.container.querySelector('section[aria-label="Status unavailable runs"]'),
    ).not.toBeNull();
    screen.press(live.querySelector<HTMLElement>('.vs-summary-card-press') as HTMLElement);
    expect(log.at(-1)).toMatchObject({
      type: 'open-run',
      host: 'local',
      runId: 'generic-live',
    });
    screen.unmount();
  });

  it('opens a card, records its exact revision as read, and removes its attention edge', () => {
    const {screen, log} = open([hostA]);
    const queue = cardOf(screen, 'Queue model');
    expect(queue.hasAttribute('data-attention')).toBe(true);
    screen.press(queue.querySelector<HTMLElement>('.vs-summary-card-press') as HTMLElement);
    expect(log.at(-1)).toMatchObject({
      type: 'open-run',
      host: 'local',
      runId: 'live-1',
      activityKey: expect.any(String),
    });
    expect(cardOf(screen, 'Queue model').hasAttribute('data-attention')).toBe(false);
    screen.unmount();
  });

  it('offers the same right-click menu for details and explicit read receipts', () => {
    const {screen, log} = open([hostA]);
    const queue = cardOf(screen, 'Queue model');
    act(() => {
      queue.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
    });
    expect(screen.controls('Open run')).toHaveLength(1);
    expect(screen.controls('Open details and description')).toHaveLength(1);
    screen.press(screen.control('Mark as read'));
    expect(log.at(-1)).toMatchObject({type: 'mark-run-read', host: 'local', runId: 'live-1'});
    expect(cardOf(screen, 'Queue model').hasAttribute('data-attention')).toBe(false);

    act(() => {
      cardOf(screen, 'Queue model').dispatchEvent(
        new MouseEvent('contextmenu', {bubbles: true, cancelable: true}),
      );
    });
    screen.press(screen.control('Mark as unread'));
    expect(log.at(-1)).toEqual({type: 'mark-run-unread', host: 'local', runId: 'live-1'});
    expect(cardOf(screen, 'Queue model').hasAttribute('data-attention')).toBe(true);
    screen.unmount();
  });

  it('opens an operator details sheet without navigating away', () => {
    const {screen, log} = open([hostA]);
    screen.press(
      cardOf(screen, 'Queue model').querySelector<HTMLElement>(
        '[aria-label^="More for"]',
      ) as HTMLElement,
    );
    screen.press(screen.control('Open details and description'));
    const dialog = screen.container.querySelector('dialog[open]');
    expect(dialog?.textContent).toContain('Make the bounded MPMC queue faster');
    expect(dialog?.textContent).toContain('Run IDlive-1');
    expect(dialog?.textContent).toContain('Latest hypothesis');
    expect(log).toHaveLength(0);
    screen.press(screen.control('Close'));
    expect(screen.container.querySelector('dialog[open]')).toBeNull();
    screen.unmount();
  });

  it('always keeps Refresh visible and reports its persistent lifecycle', () => {
    const opened = open([hostA], {local: {_tag: 'Idle'}});
    opened.screen.press(opened.screen.control('Refresh'));
    expect(opened.refreshed).toEqual(['local']);
    expect(opened.screen.controls('Refresh')).toHaveLength(1);

    opened.screen.render(opened.render({local: {_tag: 'Refreshing'}}));
    expect(opened.screen.text()).toContain('Refreshing…');
    expect(opened.screen.control('Refresh').getAttribute('aria-disabled')).toBe('true');

    opened.screen.render(opened.render({local: {_tag: 'Refreshed', sequence: 9n}}));
    expect(opened.screen.text()).toContain('Refreshed');
    expect(opened.screen.controls('Refresh')).toHaveLength(1);

    opened.screen.render(opened.render({local: {_tag: 'Failed'}}));
    expect(opened.screen.text()).toContain('Refresh failed');
    expect(opened.screen.controls('Refresh')).toHaveLength(1);
    opened.screen.unmount();
  });

  it('refreshes every shown host, or only the host selected by the operator', () => {
    const {screen, refreshed} = open();
    screen.press(screen.control('Refresh'));
    expect(refreshed).toEqual(['local', 'ssh:gpu-cluster-1']);
    refreshed.length = 0;
    const select = screen.container.querySelector<HTMLSelectElement>('select');
    if (select === null) throw new Error('no host filter');
    const View = select.ownerDocument.defaultView as unknown as {Event: typeof Event};
    act(() => {
      select.value = hostB.key;
      select.dispatchEvent(new View.Event('change', {bubbles: true}));
    });
    screen.press(screen.control('Refresh'));
    expect(refreshed).toEqual(['ssh:gpu-cluster-1']);
    screen.unmount();
  });

  it('uses waiting/unavailable language and never the removed Not read yet label', () => {
    for (const freshness of ['empty', 'unavailable'] as const) {
      const host: FleetHostInput = {
        key: 'local',
        label: 'This Mac',
        freshness,
        status: statusFor(freshness, null, freshness === 'unavailable'),
        data: emptyState,
        runIndex: EMPTY_RUN_INDEX,
      };
      const {screen} = open([host]);
      expect(screen.text()).not.toContain('Not read yet');
      expect(screen.text()).toMatch(/Waiting for runs|Run index unavailable/);
      expect(screen.controls('Refresh')).toHaveLength(1);
      expect(screen.controls('Retry')).toHaveLength(0);
      screen.unmount();
    }
  });

  it('keeps New run available when there are no hosts or no runs', () => {
    for (const hosts of [
      [],
      [
        {
          ...hostA,
          data: emptyState,
        },
      ],
    ] satisfies readonly (readonly FleetHostInput[])[]) {
      const {screen} = open(hosts);
      expect(screen.controls('New run')).toHaveLength(1);
      expect(screen.text()).toMatch(/No machines connected|No runs yet/);
      screen.unmount();
    }
  });

  it('asks the host to include truncated history in a semantic search', () => {
    const cut: FleetHostInput = {
      ...hostA,
      data: {...hostA.data, cut: {truncated: true, runsLeftOut: 7}},
    };
    const {screen} = open([cut]);
    expect(screen.text()).toContain('7 older runs lack managed details');
    changeSearch(screen, 'queue');
    expect(screen.text()).toContain('Older runs not searched yet');
    screen.unmount();
  });

  it('shows an authoritative omitted match and says whether the bounded search was complete', () => {
    const cut: FleetHostInput = {
      ...hostA,
      data: {...hostA.data, cut: {truncated: true, runsLeftOut: 7}},
    };
    const hidden = runOf({
      id: 'hidden-run',
      task: 'Archived allocator trial',
      status: RunStatus.ENDED,
      startedMs: NOW - 10 * HOUR,
      endedMs: NOW - 9 * HOUR,
    });
    const result = create(RunSearchResultSchema, {
      run: hidden,
      summary: create(RunSummarySchema, {runId: hidden.id}),
    });
    const response = create(SearchRunsResponseSchema, {
      sequence: 44n,
      results: [result],
    });
    const opened = open([cut]);
    changeSearch(opened.screen, 'hidden allocator');
    opened.screen.render(
      opened.render(undefined, {
        [cut.key]: {_tag: 'Found', query: 'hidden allocator', response},
      }),
    );

    expect(cardOf(opened.screen, 'Archived allocator trial').textContent).toContain(
      'Measurements not in search result',
    );
    expect(opened.screen.text()).toContain('All run records searched');

    opened.screen.render(
      opened.render(undefined, {
        [cut.key]: {
          _tag: 'Found',
          query: 'hidden allocator',
          response: create(SearchRunsResponseSchema, {...response, truncated: true}),
        },
      }),
    );
    expect(opened.screen.text()).toContain('More matching runs exist');
    opened.screen.unmount();
  });
});

describe('run-card graphs', () => {
  const [throughput = SERVING[0]] = SERVING;

  it('fills the chart side with every candidate and the host-marked best path', () => {
    if (throughput === undefined) throw new Error('no throughput objective');
    const measured = measuredSearch('measured-1', {
      seed: 3,
      candidates: 12,
      startMs: NOW - 5 * HOUR,
      endMs: NOW - MINUTE,
      objectives: [throughput],
    });
    const host: FleetHostInput = {
      ...hostA,
      data: stateOf([
        {
          id: 'measured-1',
          task: 'Measured queue',
          status: RunStatus.RUNNING,
          startedMs: NOW - 5 * HOUR,
          search: measured,
        },
      ]),
    };
    const {screen} = open([host]);
    const chart = cardOf(screen, 'Measured queue').querySelector('.vs-summary-card-chart');
    const svg = chart?.querySelector('svg[role="img"]');
    expect(svg?.getAttribute('aria-label')).toContain('throughput');
    expect(svg?.getAttribute('preserveAspectRatio')).toBe('none');
    expect(chart?.querySelector('.vs-spark-dots')?.getAttribute('d')?.split('M')).toHaveLength(13);
    expect(chart?.querySelector('.vs-spark-line')?.getAttribute('data-tone')).toBe('accent');
    screen.unmount();
  });

  it('uses an honest awaiting state when the host has served no series', () => {
    const {screen} = open([hostA]);
    const chart = cardOf(screen, 'Queue model').querySelector('.vs-summary-card-chart');
    expect(chart?.querySelector('svg')).toBeNull();
    expect(chart?.textContent).toContain('Awaiting measurements');
    screen.unmount();
  });
});
