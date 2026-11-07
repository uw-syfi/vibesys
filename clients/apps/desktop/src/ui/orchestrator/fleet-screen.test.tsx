import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  Direction,
  Harness,
  MessageSchema,
  ReportKind,
  ReportSchema,
  RunSchema,
  RunStatus,
  RunsSnapshotSchema,
  type SearchState,
  SearchStateSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {act, type JSX, useReducer} from 'react';
import {
  applySnapshot,
  emptyState,
  type FleetHostInput,
  initialNavigation,
  type NavigationAction,
  navigationReducer,
  type OrchestratorState,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {
  FakeDaemon,
  fleetScenario,
  measuredSearch,
  SERVING,
  serveSummaries,
} from '../../testing/orchestrator/index.js';
import {FleetScreen} from './fleet-screen.js';
import {freshnessPhrase} from './fleet-words.js';

installDom();

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
/** Sat 2026-11-07 10:00 UTC, which is 02:00 in Seattle (PST). */
const NOW = Date.UTC(2026, 10, 7, 10, 0, 0);

const noop = (): void => undefined;

/** A run, its lead, a report, and the search facts a row reads: as a host would have sent them. */
function stateOf(
  runs: readonly {
    readonly id: string;
    readonly task: string;
    readonly status: RunStatus;
    readonly startedMs: number;
    readonly endedMs?: number;
    readonly cost?: number;
    readonly budget?: number;
    readonly reportMs?: number;
    readonly working?: number;
    readonly result?: {readonly value: number; readonly unit: string; readonly percent?: number};
    /** The search the host served for the run, whole: it replaces what `result` would make. */
    readonly search?: SearchState;
  }[],
): OrchestratorState {
  const snapshot = {
    runs: runs.map(run =>
      create(RunSchema, {
        id: run.id,
        task: run.task,
        repository: 'decode-service',
        status: run.status,
        createdAt: timestampFromMs(run.startedMs),
        ...(run.endedMs === undefined ? {} : {endedAt: timestampFromMs(run.endedMs)}),
        ...(run.cost === undefined ? {} : {usage: {costUsd: run.cost}}),
        ...(run.budget === undefined ? {} : {maxCostUsd: run.budget}),
      }),
    ),
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
                case: 'report' as const,
                value: create(ReportSchema, {headline: 'h', kind: ReportKind.PROGRESS}),
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
                    case: 'current' as const,
                    value: {
                      head: 'b'.repeat(40),
                      result: {
                        outcome: {
                          case: 'measure' as const,
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
  };
  return serveSummaries(applySnapshot(emptyState, create(RunsSnapshotSchema, snapshot)));
}

const live = (id: string, task: string, more = {}) => ({
  id,
  task,
  status: RunStatus.RUNNING,
  startedMs: NOW - 5 * HOUR - 20 * MINUTE,
  ...more,
});

const hostA: FleetHostInput = {
  key: 'local',
  label: 'This Mac',
  freshness: 'live',
  staleForMs: null,
  data: stateOf([
    live('a1', 'queue-rs · mpmc', {
      cost: 318,
      budget: 500,
      reportMs: NOW - 23 * MINUTE,
      working: 3,
      result: {value: 2310, unit: 'tok/s', percent: 71.2},
    }),
    {
      id: 'a2',
      task: 'Tokenizer throughput',
      status: RunStatus.ENDED,
      startedMs: NOW - 2 * 24 * HOUR - 6 * HOUR,
      endedMs: NOW - 2 * 24 * HOUR,
      cost: 12.4,
    },
  ]),
};

const hostB: FleetHostInput = {
  key: 'ssh:gpu-cluster-1',
  label: 'gpu-cluster-1',
  freshness: 'stale',
  staleForMs: 14 * MINUTE,
  data: stateOf([live('b1', 'KV cache kernel', {cost: 1204.5})]),
};

/** The screen as the window composes it over the navigation fold, recording every intent. */
function Screen(props: {
  readonly hosts: readonly FleetHostInput[];
  readonly log: NavigationAction[];
  readonly nowMs?: number;
}): JSX.Element {
  const [navigation, move] = useReducer(navigationReducer, initialNavigation);
  return (
    <FleetScreen
      hosts={props.hosts}
      place={navigation.fleet}
      nowMs={props.nowMs ?? NOW}
      go={action => {
        props.log.push(action);
        move(action);
      }}
      retryHost={noop}
      newRun={null}
    />
  );
}

const open = (hosts: readonly FleetHostInput[], nowMs?: number) => {
  const log: NavigationAction[] = [];
  const screen = mount(
    <Screen hosts={hosts} log={log} {...(nowMs === undefined ? {} : {nowMs})} />,
  );
  return {log, screen};
};

const rows = (screen: ReturnType<typeof mount>): HTMLElement[] => [
  ...screen.container.querySelectorAll<HTMLElement>('[role="option"]'),
];
const cell = (row: HTMLElement, column: string): HTMLElement => {
  const found = row.querySelector<HTMLElement>(`[data-column="${column}"]`);
  if (found === null) throw new Error(`no ${column} cell`);
  return found;
};
const rowOf = (screen: ReturnType<typeof mount>, name: string): HTMLElement => {
  const found = rows(screen).find(row => cell(row, 'name').textContent?.includes(name));
  if (found === undefined) throw new Error(`no row for ${name}`);
  return found;
};

describe('the fleet screen', () => {
  it('lists the live runs first and then the history, from every host, one row each', () => {
    const {screen} = open([hostA, hostB]);
    const live = screen.container.querySelector('[role="listbox"][aria-label="Live runs"]');
    const history = screen.container.querySelector('[role="listbox"][aria-label="Run history"]');
    expect(
      [...(live?.querySelectorAll('[role="option"]') ?? [])].map(
        row => cell(row as HTMLElement, 'name').textContent,
      ),
    ).toEqual([
      expect.stringContaining('queue-rs · mpmc'),
      expect.stringContaining('KV cache kernel'),
    ]);
    expect(history?.querySelectorAll('[role="option"]')).toHaveLength(1);
    expect(live?.compareDocumentPosition(history as Node)).toBe(4);
    screen.unmount();
  });

  it('shows each row: host, state, best per objective with the host percent, cost of budget, agents, report age', () => {
    const {screen} = open([hostA, hostB]);
    const row = rowOf(screen, 'queue-rs');
    expect(cell(row, 'host').textContent).toContain('This Mac');
    expect(cell(row, 'status').textContent).toContain('Running');
    expect(cell(row, 'status').textContent).toContain('Report 23m ago');
    expect(cell(row, 'best').textContent).toBe('2,310 tok/s · 71%');
    expect(cell(row, 'agents').textContent).toBe('3');
    expect(cell(row, 'cost').textContent).toBe('$318of $500');
    expect(cell(row, 'name').textContent).toContain('5h 20m');
    // The row is named by its run alone, and says the rest as one description.
    expect(row.getAttribute('aria-description')).toContain('On This Mac');
    screen.unmount();
  });

  it('tells an ended run by its end time in Seattle time, and shows no agents for it', () => {
    const {screen} = open([hostA]);
    const row = rowOf(screen, 'Tokenizer');
    // NOW is Nov 7 02:00 PST; two days earlier is Nov 5 02:00 PST.
    expect(cell(row, 'status').textContent).toContain('Finished');
    expect(cell(row, 'status').textContent).toContain('Nov 5, 2:00 AM PST');
    expect(cell(row, 'agents').textContent).toBe('—');
    expect(cell(row, 'cost').textContent).toBe('$12.40');
    expect(cell(row, 'best').textContent).toBe('—');
    screen.unmount();
  });

  it('says so, inline, when a run has no report yet', () => {
    const {screen} = open([{...hostA, data: stateOf([live('a1', 'no report yet', {cost: 1})])}]);
    expect(cell(rowOf(screen, 'no report yet'), 'status').textContent).toContain('No report');
    screen.unmount();
  });

  it('keeps the rows of a stale host and says how stale it is, in its rows and its filter, with a Retry and no banner', () => {
    const {screen} = open([hostA, hostB]);
    const row = rowOf(screen, 'KV cache kernel');
    expect(cell(row, 'host').textContent).toContain('gpu-cluster-1');
    expect(cell(row, 'host').textContent).toContain('Stale 14m');
    expect(
      [...screen.container.querySelectorAll('option')].map(option => option.textContent),
    ).toContain('gpu-cluster-1 · Stale 14m');
    expect(screen.controls('Retry')).toHaveLength(1);
    expect(screen.container.querySelector('[role="alert"], .vs-banner')).toBeNull();
    screen.unmount();
  });

  it('never claims a host has no runs before it has been read', () => {
    for (const freshness of ['empty', 'unavailable'] as const) {
      const {screen} = open([
        {key: 'local', label: 'This Mac', freshness, staleForMs: null, data: emptyState},
      ]);
      expect(screen.text()).not.toContain('No runs');
      expect(screen.text()).toContain('Not read yet');
      screen.unmount();
    }
    const {screen} = open([
      {key: 'local', label: 'This Mac', freshness: 'live', staleForMs: null, data: emptyState},
    ]);
    expect(screen.text()).toContain('No runs');
    screen.unmount();
  });

  it('says No runs at the start of the line, with the one New run button inside it', () => {
    const log: NavigationAction[] = [];
    const screen = mount(
      <FleetScreen
        hosts={[
          {key: 'local', label: 'This Mac', freshness: 'live', staleForMs: null, data: emptyState},
        ]}
        place={initialNavigation.fleet}
        nowMs={NOW}
        go={action => log.push(action)}
        retryHost={noop}
        newRun={<button type="button">New run</button>}
      />,
    );
    const empty = screen.container.querySelector('.vs-empty');
    expect(empty?.getAttribute('data-align')).toBe('start');
    expect(empty?.textContent).toContain('New run');
    expect(screen.controls('New run')).toHaveLength(1);
    screen.unmount();
  });

  it('cuts a host name in its middle, so hosts that share a prefix still tell apart', () => {
    const names = ['host-b-a-vega-cluster-1', 'host-b-a-vega-cluster-2'];
    const {screen} = open(
      names.map((label, index) => ({
        key: `h${index}`,
        label,
        freshness: 'live' as const,
        staleForMs: null,
        data: stateOf([live(`x${index}`, `run ${index}`)]),
      })),
    );
    const tails = rows(screen).map(
      row => cell(row, 'host').querySelector('.vs-middle-tail')?.textContent,
    );
    expect(tails.sort()).toEqual(['luster-1', 'luster-2']);
    for (const row of rows(screen))
      expect(names).toContain(cell(row, 'host').querySelector('.vs-middle')?.getAttribute('title'));
    screen.unmount();
  });

  it('never says a host is stale for 0 seconds, and says the age once it is worth saying', () => {
    fc.assert(
      fc.property(fc.integer({min: 0, max: 30 * 24 * HOUR}), ageMs => {
        const phrase = freshnessPhrase('stale', ageMs);
        expect(phrase).not.toMatch(/\b0s\b/);
        expect(phrase === 'Stale').toBe(ageMs < 10_000);
      }),
      {seed: 31},
    );
    expect(freshnessPhrase('stale', null)).toBe('Stale');
    expect(freshnessPhrase('live', 5 * HOUR)).toBe('Live');
  });

  it('sends the state filter and the host filter as intents, and shows what they leave', () => {
    const {screen, log} = open([hostA, hostB]);
    const radio = (value: string): HTMLElement => {
      const found = screen.container.querySelector<HTMLElement>(
        `input[type="radio"][value="${value}"]`,
      );
      if (found === null) throw new Error(`no ${value} option`);
      return found;
    };
    screen.press(radio('ended'));
    expect(log.at(-1)).toEqual({type: 'filter-runs-by-state', state: 'ended'});
    expect(rows(screen).map(row => cell(row, 'name').textContent)).toEqual([
      expect.stringContaining('Tokenizer'),
    ]);
    screen.press(radio('all'));
    const hostSelect = screen.container.querySelector('select');
    if (hostSelect === null) throw new Error('no host control');
    const View = hostSelect.ownerDocument.defaultView as unknown as {Event: typeof Event};
    const chooseHost = (value: string): void => {
      act(() => {
        hostSelect.value = value;
        hostSelect.dispatchEvent(new View.Event('change', {bubbles: true}));
      });
    };
    chooseHost('ssh:gpu-cluster-1');
    expect(log.at(-1)).toEqual({type: 'filter-runs-by-host', host: 'ssh:gpu-cluster-1'});
    expect(rows(screen).map(row => cell(row, 'name').textContent)).toEqual([
      expect.stringContaining('KV cache kernel'),
    ]);
    chooseHost('');
    expect(log.at(-1)).toEqual({type: 'filter-runs-by-host', host: null});
    expect(rows(screen)).toHaveLength(3);
    screen.unmount();
  });

  it('opens the run a press or Enter on its row picks, naming the host and the run', () => {
    const {screen, log} = open([hostA, hostB]);
    screen.press(rowOf(screen, 'KV cache kernel'));
    expect(log.at(-1)).toEqual({type: 'open-run', host: 'ssh:gpu-cluster-1', runId: 'b1'});
    const row = rowOf(screen, 'queue-rs');
    const Key = (row.ownerDocument.defaultView as unknown as {KeyboardEvent: typeof KeyboardEvent})
      .KeyboardEvent;
    row.dispatchEvent(new Key('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
    expect(log.at(-1)).toEqual({type: 'open-run', host: 'local', runId: 'a1'});
    screen.unmount();
  });

  it('marks the row that was opened as selected when the list comes back', () => {
    const log: NavigationAction[] = [];
    const screen = mount(<Screen hosts={[hostA, hostB]} log={log} />);
    screen.press(rowOf(screen, 'queue-rs'));
    // The run screen is the caller's; coming back is the list drawn again with the same navigation.
    expect(rowOf(screen, 'queue-rs').getAttribute('aria-selected')).toBe('true');
    expect(rowOf(screen, 'KV cache kernel').getAttribute('aria-selected')).toBe('false');
    screen.unmount();
  });

  it('cuts a long run name or host name short and keeps the whole text for the pointer', () => {
    const name = `${'a-run-with-a-very-long-name-'.repeat(8)}end`;
    const host = `ssh:${'a-host-with-a-very-long-name-'.repeat(6)}`;
    const {screen} = open([
      {
        key: host,
        label: host,
        freshness: 'live',
        staleForMs: null,
        data: stateOf([live('x1', name, {cost: 1})]),
      },
    ]);
    const row = rows(screen)[0];
    if (row === undefined) throw new Error('no row');
    expect(cell(row, 'name').getAttribute('title')).toContain(name);
    expect(cell(row, 'host').getAttribute('title')).toContain(host);
    screen.unmount();
  });

  it('draws only the rows near the view, however many runs there are', () => {
    const scenario = fleetScenario({seed: 5, prefix: 'big', runs: 600, live: 3, nowMs: NOW});
    const data = applySnapshot(emptyState, new FakeDaemon(scenario).snapshot());
    const {screen} = open([
      {key: 'local', label: 'This Mac', freshness: 'live', staleForMs: null, data},
    ]);
    const history = screen.container.querySelector('[role="listbox"][aria-label="Run history"]');
    const drawn = history?.querySelectorAll('[role="option"]') ?? [];
    expect(drawn.length).toBeGreaterThan(0);
    expect(drawn.length).toBeLessThan(60);
    expect(drawn[0]?.getAttribute('aria-setsize')).toBe('597');
    // The scenario's runs have series, and only the rows drawn draw them.
    expect(history?.querySelectorAll('.vs-spark').length).toBeGreaterThan(0);
    expect(history?.querySelectorAll('.vs-spark').length).toBeLessThanOrEqual(drawn.length);
    screen.unmount();
  });

  it('flashes a row for a run that began or ended just now, and no other', () => {
    const fresh = {
      ...hostA,
      data: stateOf([
        {id: 'n1', task: 'just started', status: RunStatus.RUNNING, startedMs: NOW - 3_000},
        {id: 'n2', task: 'long running', status: RunStatus.RUNNING, startedMs: NOW - HOUR},
        {
          id: 'n3',
          task: 'just ended',
          status: RunStatus.ENDED,
          startedMs: NOW - HOUR,
          endedMs: NOW - 2_000,
        },
        {
          id: 'n4',
          task: 'ended earlier',
          status: RunStatus.ENDED,
          startedMs: NOW - 2 * HOUR,
          endedMs: NOW - HOUR,
        },
      ]),
    };
    const {screen} = open([fresh]);
    const flashing = rows(screen)
      .filter(row => row.hasAttribute('data-fresh'))
      .map(row => cell(row, 'name').textContent);
    expect(flashing).toEqual([
      expect.stringContaining('just started'),
      expect.stringContaining('just ended'),
    ]);
    screen.unmount();
  });
});

const measuredRun = (id: string, task: string, objectives: typeof SERVING, more = {}) => ({
  id,
  task,
  status: RunStatus.RUNNING,
  startedMs: NOW - 5 * HOUR,
  ...more,
  search: measuredSearch(id, {
    seed: 3,
    candidates: 12,
    startMs: NOW - 5 * HOUR,
    endMs: NOW - MINUTE,
    objectives,
  }),
});

/** Candidates as a host serves them: oldest first, each a moment later, the marks arbitrary. */
const served = fc
  .array(
    fc.record({
      value: fc.integer({min: -500, max: 500}),
      bestSoFar: fc.boolean(),
      gapSeconds: fc.integer({min: 1, max: 600}),
    }),
    {minLength: 1, maxLength: 30},
  )
  .map(rows => {
    let atMs = NOW - 6 * HOUR;
    return rows.map(row => {
      atMs += row.gapSeconds * 1000;
      return {atMs, value: row.value, bestSoFar: row.bestSoFar};
    });
  });

interface Plotted {
  readonly x: number;
  readonly y: number;
}

/** The corners of a step line (`M x y H x V y ... H end`), the closing run to the edge left out. */
function vertices(d: string): Plotted[] {
  const found: Plotted[] = [];
  let x = 0;
  for (const [, command, args = ''] of d.matchAll(/([MHV])([^MHV]*)/g)) {
    const numbers = args.trim().split(/\s+/).map(Number);
    if (command === 'M') {
      x = numbers[0] ?? 0;
      found.push({x, y: numbers[1] ?? 0});
    } else if (command === 'H') x = numbers[0] ?? 0;
    else found.push({x, y: numbers[0] ?? 0});
  }
  return found;
}

/** The dots of a plot (`M x yh0` each). */
const dotsOf = (d: string): Plotted[] =>
  [...d.matchAll(/M([-\d.e+]+) ([-\d.e+]+)h0/g)].map(([, x, y]) => ({
    x: Number(x),
    y: Number(y),
  }));

/**
 * The plot is the served points under a map that keeps time's order and reverses the value's (up is
 * better to the eye, the screen's y grows down): every pair compares the same way in both.
 */
function expectSameShape(
  drawn: readonly Plotted[],
  given: readonly {readonly atMs: number; readonly value: number}[],
): void {
  expect(drawn).toHaveLength(given.length);
  for (const [i, a] of drawn.entries())
    for (const [j, b] of drawn.entries()) {
      const p = given[i];
      const q = given[j];
      if (p === undefined || q === undefined) throw new Error('no point');
      expect(Math.sign(a.y - b.y)).toBe(0 - Math.sign(p.value - q.value));
      expect(Math.sign(a.x - b.x)).toBe(Math.sign(p.atMs - q.atMs));
    }
}

const trendOf = (screen: ReturnType<typeof mount>, name: string): HTMLElement =>
  cell(rowOf(screen, name), 'trend');

const withHost = (runs: Parameters<typeof stateOf>[0]): FleetHostInput => ({
  key: 'local',
  label: 'This Mac',
  freshness: 'live',
  staleForMs: null,
  data: stateOf(runs),
});

describe('the candidates cell of the fleet screen', () => {
  const [throughput = SERVING[0], latency = SERVING[1]] = SERVING;

  it('draws one objective as every candidate and the best-so-far line, with nothing else in the cell', () => {
    if (throughput === undefined) throw new Error('no objective');
    const {screen} = open([withHost([measuredRun('m1', 'one objective', [throughput])])]);
    const trend = trendOf(screen, 'one objective');
    const chart = trend.querySelector('svg[role="img"]');
    expect(chart?.getAttribute('aria-label')).toContain('throughput');
    // 12 candidates are 12 dots; the line is the points the host marked.
    expect(trend.querySelector('.vs-spark-dots')?.getAttribute('d')?.split('M').length).toBe(13);
    expect(trend.querySelector('.vs-spark-line')).not.toBeNull();
    expect(trend.textContent).toBe('');
    screen.unmount();
  });

  it('draws the first of two objectives and counts the other', () => {
    if (throughput === undefined || latency === undefined) throw new Error('no objective');
    const {screen} = open([withHost([measuredRun('m2', 'two objectives', [throughput, latency])])]);
    const trend = trendOf(screen, 'two objectives');
    expect(trend.querySelectorAll('svg[role="img"]')).toHaveLength(1);
    expect(trend.querySelector('svg')?.getAttribute('aria-label')).toContain('throughput');
    expect(trend.textContent).toBe('+1');
    screen.unmount();
  });

  it('draws a line in the accent for a run that is working and a quiet one for a run that is over', () => {
    if (throughput === undefined) throw new Error('no objective');
    const {screen} = open([
      withHost([
        measuredRun('m3', 'working run', [throughput]),
        measuredRun('m4', 'finished run', [throughput], {
          status: RunStatus.ENDED,
          endedMs: NOW - HOUR,
        }),
      ]),
    ]);
    const toneOf = (name: string) =>
      trendOf(screen, name).querySelector('.vs-spark-line')?.getAttribute('data-tone');
    expect(toneOf('working run')).toBe('accent');
    expect(toneOf('finished run')).toBe('muted');
    screen.unmount();
  });

  it('is empty for a run with nothing measured, with no placeholder', () => {
    const {screen} = open([
      withHost([
        {
          id: 'm5',
          task: 'nothing measured',
          status: RunStatus.RUNNING,
          startedMs: NOW - HOUR,
          search: measuredSearch('m5', {
            seed: 1,
            candidates: 0,
            startMs: NOW - HOUR,
            endMs: NOW,
            objectives: SERVING,
          }),
        },
      ]),
    ]);
    const trend = trendOf(screen, 'nothing measured');
    expect(trend.querySelector('svg')).toBeNull();
    expect(trend.textContent).toBe('');
    screen.unmount();
  });

  it('is empty for a run of a host that serves no series, though it shows the best and the bound', () => {
    const {screen} = open([hostA]);
    const row = rowOf(screen, 'queue-rs');
    expect(cell(row, 'best').textContent).toBe('2,310 tok/s · 71%');
    expect(cell(row, 'trend').querySelector('svg')).toBeNull();
    expect(cell(row, 'trend').textContent).toBe('');
    screen.unmount();
  });

  it('draws exactly the best-so-far points the host marked, and every candidate as a dot, whatever the values say', () => {
    fc.assert(
      fc.property(
        served,
        fc.constantFrom(Direction.MAXIMIZE, Direction.MINIMIZE),
        (points, direction) => {
          const search = create(SearchStateSchema, {
            runId: 'p1',
            series: [
              {
                objective: 'score',
                direction,
                points: points.map((point, index) => ({
                  jobId: `j${index}`,
                  commit: 'c',
                  at: timestampFromMs(point.atMs),
                  value: point.value,
                  bestSoFar: point.bestSoFar,
                })),
              },
            ],
          });
          const {screen} = open([
            withHost([
              {
                id: 'p1',
                task: 'property run',
                status: RunStatus.RUNNING,
                startedMs: NOW - 7 * HOUR,
                search,
              },
            ]),
          ]);
          try {
            const trend = trendOf(screen, 'property run');
            const marked = points.filter(point => point.bestSoFar);
            const line = trend.querySelector('.vs-spark-line')?.getAttribute('d') ?? null;
            if (marked.length === 0) expect(line).toBeNull();
            else expectSameShape(vertices(line ?? ''), marked);
            const dots = trend.querySelector('.vs-spark-dots')?.getAttribute('d') ?? '';
            expectSameShape(dotsOf(dots), points);
          } finally {
            screen.unmount();
          }
        },
      ),
      {seed: 21, numRuns: 40},
    );
  });
  it('says how many older runs the host left out, as the host said it, and nothing when it listed every run', () => {
    const cut = (runsLeftOut: number): FleetHostInput => ({
      ...hostA,
      data: {...hostA.data, cut: {truncated: runsLeftOut > 0, runsLeftOut}},
    });
    const some = open([cut(7)]);
    expect(some.screen.text()).toContain('7 older runs not shown');
    some.screen.unmount();
    const none = open([cut(0)]);
    // Its room is kept, empty and hidden, so the chip never moves what is under it.
    expect(none.screen.container.querySelector('.vs-reserve [class~="vs-chip"]')).not.toBeNull();
    expect(none.screen.container.querySelector('.vs-reserve')?.getAttribute('aria-hidden')).toBe(
      'true',
    );
    expect(
      [...none.screen.container.querySelectorAll('.vs-chip')].filter(
        chip => chip.closest('.vs-reserve') === null && chip.textContent?.includes('not shown'),
      ),
    ).toHaveLength(0);
    none.screen.unmount();
    // Two hosts: the figure is of the hosts shown, and the host filter narrows it.
    const both = open([
      cut(7),
      {...hostB, data: {...hostB.data, cut: {truncated: true, runsLeftOut: 5}}},
    ]);
    expect(both.screen.text()).toContain('12 older runs not shown');
    both.screen.unmount();
  });

  it('has the same cells in its filter row whether or not a host has answered late, so the rows below never shift', () => {
    const bar = (hosts: readonly FleetHostInput[]) => {
      const opened = open(hosts);
      const row = opened.screen.container.querySelector('[aria-label="Show"]')?.parentElement;
      const cells = [...(row?.children ?? [])].map(each => each.tagName);
      opened.screen.unmount();
      return cells;
    };
    const cut = (runsLeftOut: number): FleetHostInput => ({
      ...hostA,
      data: {...hostA.data, cut: {truncated: runsLeftOut > 0, runsLeftOut}},
    });
    const quiet = bar([cut(0)]);
    expect(quiet.length).toBeGreaterThanOrEqual(3);
    // A count of older runs, and a stale host with its Retry, arrive: no cell is added or removed.
    expect(bar([cut(7)])).toHaveLength(quiet.length);
    // The second host answers late, stale or with runs left out: the same cells as when it is live.
    const both = bar([hostA, {...hostB, freshness: 'live', staleForMs: null}]);
    expect(
      bar([hostA, {...hostB, freshness: 'empty', staleForMs: null, data: emptyState}]),
    ).toHaveLength(both.length);
    expect(bar([hostA, hostB])).toHaveLength(both.length);
    expect(
      bar([
        hostA,
        {
          ...hostB,
          freshness: 'live',
          staleForMs: null,
          data: {...hostB.data, cut: {truncated: true, runsLeftOut: 4}},
        },
      ]),
    ).toHaveLength(both.length);
  });
});
