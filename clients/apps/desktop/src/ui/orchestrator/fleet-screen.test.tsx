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
  SearchStateSchema,
} from '@vibesys/orchestrator-proto';
import {type JSX, useReducer} from 'react';
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
import {FakeDaemon, fleetScenario, serveSummaries} from '../../testing/orchestrator/index.js';
import {FleetScreen} from './fleet-screen.js';

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
      run.result === undefined
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
                  ...(run.result.percent === undefined ? {} : {percentOfBound: run.result.percent}),
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
      screen.container.querySelector('label[data-label="gpu-cluster-1 · Stale 14m"]'),
    ).not.toBeNull();
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
    screen.press(radio('ssh:gpu-cluster-1'));
    expect(log.at(-1)).toEqual({type: 'filter-runs-by-host', host: 'ssh:gpu-cluster-1'});
    expect(rows(screen).map(row => cell(row, 'name').textContent)).toEqual([
      expect.stringContaining('KV cache kernel'),
    ]);
    screen.press(radio(''));
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
