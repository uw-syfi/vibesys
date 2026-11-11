import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AcceleratorVendor,
  AgentRole,
  AgentSchema,
  AgentStatus,
  BoundUnitCheck,
  Direction,
  Harness,
  MessageSchema,
  ReportKind,
  ReportSchema,
  type Run,
  RunLimit,
  RunSchema,
  RunStatus,
  RunsSnapshotSchema,
  type SearchState,
  SearchStateSchema,
  type TaskCard,
  TaskCardSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {act, type JSX, useReducer} from 'react';
import {
  applySnapshot,
  DORMANT_GENERIC_RUN_INDEX,
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
  REFUSAL_CAUSES,
  type RefusalCause,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {
  FakeDaemon,
  fleetScenario,
  measuredSearch,
  SERVING,
  serveSummaries,
} from '../../testing/orchestrator/index.js';
import {DisplayClockProvider} from '../display-clock.js';
import {hostStatusWord} from '../host-status-words.js';
import {causeWord} from '../refusal-words.js';
import {TEST_ZONE} from '../test-display-clock.js';
import {FleetScreen} from './fleet-screen.js';

installDom();

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
/** Sat 2026-11-07 10:00 UTC, which is 02:00 in Seattle (PST). */
const NOW = Date.UTC(2026, 10, 7, 10, 0, 0);

const noop = (): void => undefined;

/** One run as the test states it: what a row reads of it, the rest left at its default. */
interface RunFacts {
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
  /** The task card the host stored on the run. */
  readonly card?: TaskCard;
  readonly repository?: string;
  readonly stalledMs?: number;
  readonly limit?: RunLimit;
  readonly limitMs?: number;
  readonly finalReported?: boolean;
  /** How many working copies the run kept. */
  readonly kept?: number;
  readonly deadlineMs?: number;
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
    ...(run.card === undefined ? {} : {taskCard: run.card}),
    ...(run.stalledMs === undefined ? {} : {stalledSince: timestampFromMs(run.stalledMs)}),
    limitReached: run.limit ?? RunLimit.UNSPECIFIED,
    ...(run.limitMs === undefined ? {} : {limitReachedAt: timestampFromMs(run.limitMs)}),
    finalReported: run.finalReported ?? false,
    // Where a checkout is kept is a path on the host; the list reads only how many there are.
    keptCheckouts: Array.from({length: run.kept ?? 0}, (_, index) => ({agentId: `w${index}`})),
    ...(run.deadlineMs === undefined ? {} : {deadline: timestampFromMs(run.deadlineMs)}),
  });

/** A run, its lead, a report, and the search facts a row reads: as a host would have sent them. */
function stateOf(runs: readonly RunFacts[]): OrchestratorState {
  const snapshot = {
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
                    // The unit the measurements are counted in, as the host serves it.
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

/** The status a host of this freshness (and refusal) has, as the service layer works it out. */
const statusFor = (
  freshness: Freshness,
  staleForMs: number | null,
  cause: RefusalCause | null,
): HostStatus => {
  if (cause !== null) return {kind: 'refused', cause, staleForMs: null};
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

const withHost = (runs: Parameters<typeof stateOf>[0]): FleetHostInput => ({
  key: 'local',
  label: 'This Mac',
  freshness: 'live',
  status: statusFor('live', null, null),
  data: stateOf(runs),
  runIndex: DORMANT_GENERIC_RUN_INDEX,
});

const readyGenericIndex = (freshness: 'current' | 'stale' = 'current'): GenericRunIndex => ({
  _tag: 'Ready',
  freshness,
  runs: new Map(),
});

const genericEntry = (runId: string): GenericRunEntry => ({
  runId,
  projectRoot: `/projects/${runId}`,
  state: 'running',
  activity: 'live',
  objective: null,
  costUsd: null,
  needsUser: false,
});

const hostA: FleetHostInput = {
  key: 'local',
  label: 'This Mac',
  freshness: 'live',
  status: statusFor('live', null, null),
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
  runIndex: DORMANT_GENERIC_RUN_INDEX,
};

const hostB: FleetHostInput = {
  key: 'ssh:gpu-cluster-1',
  label: 'gpu-cluster-1',
  freshness: 'stale',
  status: statusFor('stale', 14 * MINUTE, null),
  data: stateOf([live('b1', 'KV cache kernel', {cost: 1204.5})]),
  runIndex: DORMANT_GENERIC_RUN_INDEX,
};

/** The screen as the window composes it over the navigation fold, recording every intent. */
function Screen(props: {
  readonly hosts: readonly FleetHostInput[];
  readonly log: NavigationAction[];
  readonly nowMs?: number;
}): JSX.Element {
  const [navigation, move] = useReducer(navigationReducer, initialNavigation);
  return (
    <DisplayClockProvider zone={TEST_ZONE}>
      <FleetScreen
        hosts={props.hosts}
        place={navigation.fleet}
        nowMs={props.nowMs ?? NOW}
        go={action => {
          props.log.push(action);
          move(action);
        }}
        retryHost={noop}
        newRun={<button type="button">New run</button>}
      />
    </DisplayClockProvider>
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

  it('shows each row: repository, host, state and for how long, best per objective with the host percent, cost of budget, agents, report age', () => {
    const {screen} = open([hostA, hostB]);
    const row = rowOf(screen, 'queue-rs');
    expect(cell(row, 'host').textContent).toContain('This Mac');
    expect(cell(row, 'status').textContent).toContain('Running');
    expect(cell(row, 'status').textContent).toContain('Report 23m ago');
    expect(cell(row, 'best').textContent).toBe('2.31k tok/s · 71%');
    expect(cell(row, 'agents').textContent).toBe('3');
    expect(cell(row, 'cost').textContent).toBe('$318of $500');
    expect(cell(row, 'name').textContent).toContain('decode-service');
    expect(cell(row, 'status').querySelector('.vs-chip')?.textContent).toBe('Running 5h 20m');
    expect(cell(row, 'status').querySelector('.vs-chip')?.getAttribute('title')).toBe(
      'Running 5h 20m',
    );
    expect(cell(row, 'status').getAttribute('title')).toContain('Running, started');
    expect(cell(row, 'host').getAttribute('title')).toContain('This Mac');
    // A live run's chip grows with its running time; it is cut with an ellipsis, not left to
    // overflow the narrow column's track into Best.
    expect(cell(row, 'status').querySelector('.vs-chip')?.hasAttribute('data-clip')).toBe(true);
    // The row is named by its run alone, and says the rest as one description.
    expect(row.getAttribute('aria-description')).toContain('On This Mac');
    expect(row.getAttribute('aria-description')).toContain('Running 5h 20m');
    screen.unmount();
  });

  it('names the repository by its last name and keeps the whole of it for the pointer', () => {
    const {screen} = open([
      withHost([live('r1', 'path run', {repository: './work/decode-service'})]),
    ]);
    const name = cell(rowOf(screen, 'path run'), 'name');
    expect(name.textContent).toContain('decode-service');
    expect(name.textContent).not.toContain('./work/');
    expect(name.getAttribute('title')).toContain('./work/decode-service');
    screen.unmount();
  });

  it('labels the percent in the Best header with what it is of', () => {
    const {screen} = open([hostA]);
    const note = screen.container.querySelector(
      '.vs-columnlist-headcell[title="Best"] .vs-columnlist-note',
    );
    expect(note?.textContent).toBe('% of bound');
    expect(note?.hasAttribute('data-wide-only')).toBe(true);
    expect(
      screen.container.querySelector('[data-key="best"] button [data-narrow-only]')?.textContent,
    ).toBe('Bound %');
    expect(note?.getAttribute('title')).toContain('how close it is to the bound');
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
    expect(cell(row, 'trend').textContent).toBe('—');
    screen.unmount();
  });

  it('keeps candidate activity and the agent count in a narrow list, while Host moves into Run', () => {
    const {screen} = open([hostA]);
    const header = screen.container.querySelector('.vs-columnlist-head');
    expect(header?.querySelector('[data-key="host"]')?.hasAttribute('data-optional')).toBe(true);
    for (const key of ['trend', 'agents']) {
      expect(header?.querySelector(`[data-key="${key}"]`)?.hasAttribute('data-optional')).toBe(
        false,
      );
    }
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
    // In a narrow window the Host column gives way; the run's note keeps how stale the host is.
    expect(cell(row, 'name').querySelector('[data-narrow-only]')?.textContent).toBe(
      ' · gpu-cluster-1 · Stale 14m',
    );
    expect(
      [...screen.container.querySelectorAll('option')].map(option => option.textContent),
    ).toContain('gpu-cluster-1 · Managed details · Stale 14m · Generic index · Opening');
    expect(screen.controls('Retry managed')).toHaveLength(1);
    expect(screen.container.querySelector('[role="alert"], .vs-banner')).toBeNull();
    screen.unmount();
  });

  it('claims there are no runs only after both disjoint indexes prove it', () => {
    const opening = open([
      {
        key: 'local',
        label: 'This Mac',
        freshness: 'live',
        status: statusFor('live', null, null),
        data: emptyState,
        runIndex: DORMANT_GENERIC_RUN_INDEX,
      },
    ]);
    expect(opening.screen.text()).not.toContain('No runs');
    expect(opening.screen.text()).toContain('Not read yet');
    opening.screen.unmount();

    const unavailable = open([
      {
        key: 'local',
        label: 'This Mac',
        freshness: 'unavailable',
        status: statusFor('unavailable', null, null),
        data: emptyState,
        runIndex: DORMANT_GENERIC_RUN_INDEX,
      },
    ]);
    expect(unavailable.screen.text()).not.toContain('No runs');
    expect(unavailable.screen.text()).toContain('Run index unavailable');
    unavailable.screen.unmount();

    const complete = open([
      {
        key: 'local',
        label: 'This Mac',
        freshness: 'live',
        status: statusFor('live', null, null),
        data: emptyState,
        runIndex: readyGenericIndex(),
      },
    ]);
    expect(complete.screen.text()).toContain('No runs');
    complete.screen.unmount();
  });

  it('names stale and unavailable generic indexes instead of overstating an empty list', () => {
    const stale = open([
      {
        key: 'local',
        label: 'This Mac',
        freshness: 'live',
        status: statusFor('live', null, null),
        data: emptyState,
        runIndex: readyGenericIndex('stale'),
      },
    ]);
    expect(stale.screen.text()).toContain('Run count unconfirmed');
    stale.screen.unmount();

    const unavailable = open([
      {
        key: 'local',
        label: 'This Mac',
        freshness: 'live',
        status: statusFor('live', null, null),
        data: emptyState,
        runIndex: {_tag: 'Unavailable', reason: 'unsupported'},
      },
    ]);
    expect(unavailable.screen.text()).toContain('Run index unavailable');
    expect(unavailable.screen.text()).toContain(
      'This host does not support generic run discovery.',
    );
    unavailable.screen.unmount();
  });

  it('keeps each discovery source explicit beside nonempty rows and retries only managed data', () => {
    const generic = genericEntry('generic-only');
    const managedUnavailable = open([
      {
        key: 'local',
        label: 'This Mac',
        freshness: 'unavailable',
        status: statusFor('unavailable', null, null),
        data: emptyState,
        runIndex: {_tag: 'Ready', freshness: 'current', runs: new Map([[generic.runId, generic]])},
      },
    ]);
    expect(managedUnavailable.screen.text()).toContain('Managed details · Unavailable');
    expect(managedUnavailable.screen.text()).toContain('Up to date');
    expect(managedUnavailable.screen.controls('Retry managed')).toHaveLength(1);
    managedUnavailable.screen.unmount();

    const genericUnavailable = open([
      {...hostA, runIndex: {_tag: 'Unavailable', reason: 'unsupported'}},
    ]);
    expect(genericUnavailable.screen.text()).toContain('Generic index · Unavailable');
    expect(
      genericUnavailable.screen.container.querySelector(
        '.vs-chip[aria-description="This host does not support generic run discovery."]',
      ),
    ).not.toBeNull();
    expect(rows(genericUnavailable.screen)).not.toHaveLength(0);
    expect(
      genericUnavailable.screen
        .controls('Retry managed')
        .filter(control => control.closest('.vs-reserve') === null),
    ).toHaveLength(0);
    genericUnavailable.screen.unmount();
  });

  it('says why a host that refuses for good cannot be read, in a short label of its own, with no banner', () => {
    for (const cause of REFUSAL_CAUSES) {
      const label = causeWord(cause);
      // Its only host, never read: the label is the host's chip and what the empty list says.
      const only = open([
        {
          key: 'ssh:h',
          label: 'host-a',
          freshness: 'unavailable',
          status: statusFor('unavailable', null, cause),
          data: emptyState,
          runIndex: DORMANT_GENERIC_RUN_INDEX,
        },
      ]);
      const text = only.screen.text();
      expect(text, cause).toContain(label);
      expect(text, cause).not.toContain('Not read yet');
      expect(text, cause).not.toContain('Unavailable');
      expect(only.screen.controls('Retry managed'), cause).toHaveLength(1);
      expect(only.screen.container.querySelector('[role="alert"], .vs-banner'), cause).toBeNull();
      only.screen.unmount();
      // Beside another host: the host filter names the cause, and the other host is still read.
      const beside = open([
        {
          key: 'ssh:h',
          label: 'host-a',
          freshness: 'unavailable',
          status: statusFor('unavailable', null, cause),
          data: emptyState,
          runIndex: DORMANT_GENERIC_RUN_INDEX,
        },
        hostA,
      ]);
      expect(
        [...beside.screen.container.querySelectorAll('option')].map(option => option.textContent),
        cause,
      ).toContain(`host-a · Managed details · ${label} · Generic index · Opening`);
      beside.screen.unmount();
    }
  });

  it('still says Not read yet for a host that is being read and cannot run a run', () => {
    const {screen} = open([
      {
        key: 'local',
        label: 'This Mac',
        freshness: 'empty',
        status: statusFor('empty', null, 'job-tool-too-old'),
        data: emptyState,
        runIndex: DORMANT_GENERIC_RUN_INDEX,
      },
    ]);
    expect(screen.text()).toContain('Not read yet');
    expect(screen.text()).toContain('Job tool too old');
    screen.unmount();
  });

  it('shows a host that is read but cannot run a run with its cause on its chip, and keeps its rows', () => {
    const {screen} = open([{...hostA, status: statusFor('live', null, 'job-tool-too-old')}]);
    expect(screen.text()).toContain('Job tool too old');
    expect(rows(screen)).not.toHaveLength(0);
    // In a narrow window the Host column gives way; each run's note keeps the cause.
    const row = rowOf(screen, 'queue-rs');
    expect(cell(row, 'name').querySelector('[data-narrow-only]')?.textContent).toBe(
      ' · This Mac · Job tool too old',
    );
    expect(row.getAttribute('aria-description')).toContain('On This Mac · Job tool too old');
    screen.unmount();
  });

  it('says what the filter leaves, as text at the start of the line, and keeps New run in the header', () => {
    const empty: FleetHostInput = {
      key: 'local',
      label: 'This Mac',
      freshness: 'live',
      status: statusFor('live', null, null),
      data: emptyState,
      runIndex: readyGenericIndex(),
    };
    const titles = {all: 'No runs', live: 'No live runs', ended: 'No ended runs'} as const;
    for (const filter of ['all', 'live', 'ended'] as const) {
      const {screen} = open([empty]);
      const radio = screen.container.querySelector<HTMLElement>(`input[value="${filter}"]`);
      if (radio === null) throw new Error(`no ${filter} option`);
      screen.press(radio);
      const emptied = screen.container.querySelector('.vs-empty');
      expect(emptied?.getAttribute('data-align')).toBe('start');
      expect(emptied?.textContent).toBe(titles[filter]);
      expect(emptied?.querySelector('button')).toBeNull();
      expect(screen.controls('New run')).toHaveLength(1);
      const header = screen.container.querySelector('.vs-page-header');
      expect(header?.querySelector('button')?.textContent).toBe('New run');
      screen.unmount();
    }
  });

  it('keeps New run at one place in the header whatever the filter and whether or not any run is listed', () => {
    const {screen} = open([hostA]);
    const placeOf = (): string => {
      const header = screen.container.querySelector('.vs-page-header');
      const button = header?.querySelector('button');
      if (header === null || header === undefined || button === null || button === undefined)
        throw new Error('New run is not in the header');
      return [...header.querySelectorAll('*')].indexOf(button).toString();
    };
    const first = placeOf();
    for (const filter of ['live', 'ended', 'all', 'ended', 'live']) {
      const radio = screen.container.querySelector<HTMLElement>(`input[value="${filter}"]`);
      if (radio === null) throw new Error(`no ${filter} option`);
      screen.press(radio);
      expect(placeOf()).toBe(first);
      expect(screen.controls('New run')).toHaveLength(1);
    }
    screen.unmount();
  });

  it('sorts a list by the header pressed, then the other way, then back to its own order, in a slot that stays', () => {
    const {screen} = open([hostA, hostB]);
    const names = (): string[] =>
      rows(screen)
        .filter(each => each.closest('[aria-label="Live runs"]') !== null)
        .map(each => cell(each, 'name').textContent ?? '');
    const own = names();
    const head = (): Element | null =>
      screen.container
        .querySelector('[aria-label="Live runs"]')
        ?.parentElement?.querySelector('[data-key="name"]') ?? null;
    const slots = (): number => screen.container.querySelectorAll('.vs-columnlist-sortmark').length;
    const before = slots();
    screen.press(screen.controls('Sort by Run')[0] as HTMLElement);
    expect(names()).toEqual([...own].sort((a, b) => a.localeCompare(b)));
    expect(head()?.getAttribute('data-sorted')).toBe('asc');
    screen.press(screen.controls('Sort by Run')[0] as HTMLElement);
    expect(names()).toEqual([...own].sort((a, b) => b.localeCompare(a)));
    screen.press(screen.controls('Sort by Run')[0] as HTMLElement);
    expect(names()).toEqual(own);
    expect(slots()).toBe(before);
    screen.unmount();
  });

  it('cuts the model of a long run title and keeps its hardware whole', () => {
    const card = create(TaskCardSchema, {
      task: 'flint',
      modelName: `Flint ${'very-long-model-name-'.repeat(8)}`,
      hardware: {vendor: AcceleratorVendor.AMD, count: 1},
      hardwareName: 'AMD MI300X',
    });
    const {screen} = open([
      {
        key: 'local',
        label: 'This Mac',
        freshness: 'live',
        status: statusFor('live', null, null),
        data: stateOf([live('x1', 'flint', {cost: 1, card})]),
        runIndex: DORMANT_GENERIC_RUN_INDEX,
      },
    ]);
    const row = rows(screen)[0];
    if (row === undefined) throw new Error('no row');
    const lead = cell(row, 'name').querySelector('.vs-columnlist-split-lead');
    const tail = cell(row, 'name').querySelector('.vs-columnlist-split-tail');
    expect(lead?.textContent).toBe(card.modelName);
    expect(tail?.textContent).toBe(' \u00b7 1\u00d7 MI300X');
    expect(cell(row, 'name').getAttribute('title')).toContain(
      `${card.modelName} \u00b7 1\u00d7 MI300X`,
    );
    screen.unmount();
  });

  it('draws a title with no hardware as one part', () => {
    const {screen} = open([hostA]);
    const row = rowOf(screen, 'queue-rs');
    expect(row.querySelector('.vs-columnlist-split')).toBeNull();
    screen.unmount();
  });

  it('cuts a host name in its middle, so hosts that share a prefix still tell apart', () => {
    const names = ['host-b-a-vega-cluster-1', 'host-b-a-vega-cluster-2'];
    const {screen} = open(
      names.map((label, index) => ({
        key: `h${index}`,
        label,
        freshness: 'live' as const,
        status: statusFor('live', null, null),
        data: stateOf([live(`x${index}`, `run ${index}`)]),
        runIndex: DORMANT_GENERIC_RUN_INDEX,
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
        const {label} = hostStatusWord({kind: 'stale', cause: null, staleForMs: ageMs});
        expect(label).not.toMatch(/\b0s\b/);
        expect(label === 'Stale').toBe(ageMs < 10_000);
      }),
      {seed: 31},
    );
    expect(hostStatusWord({kind: 'stale', cause: null, staleForMs: null}).label).toBe('Stale');
    expect(hostStatusWord({kind: 'live', cause: null, staleForMs: 5 * HOUR}).label).toBe('Live');
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
        status: statusFor('live', null, null),
        data: stateOf([live('x1', name, {cost: 1})]),
        runIndex: DORMANT_GENERIC_RUN_INDEX,
      },
    ]);
    const row = rows(screen)[0];
    if (row === undefined) throw new Error('no row');
    expect(cell(row, 'name').getAttribute('title')).toContain(name);
    expect(cell(row, 'host').getAttribute('title')).toContain(host);
    screen.unmount();
  });

  it('draws only the rows near the view, however many runs there are', () => {
    const LIVE = 3;
    // What a host of `runs` runs draws: the rows on screen, the history size one of them names, and
    // the series, which only the drawn rows draw.
    const shownOf = (runs: number): {drawn: number; setsize: string | null; sparks: number} => {
      const scenario = fleetScenario({seed: 5, prefix: 'big', runs, live: LIVE, nowMs: NOW});
      const data = applySnapshot(emptyState, new FakeDaemon(scenario).snapshot());
      const {screen} = open([
        {
          key: 'local',
          label: 'This Mac',
          freshness: 'live',
          status: statusFor('live', null, null),
          data,
          runIndex: DORMANT_GENERIC_RUN_INDEX,
        },
      ]);
      try {
        const history = screen.container.querySelector(
          '[role="listbox"][aria-label="Run history"]',
        );
        const drawn = [...(history?.querySelectorAll('[role="option"]') ?? [])];
        return {
          drawn: drawn.length,
          setsize: drawn[0]?.getAttribute('aria-setsize') ?? null,
          sparks: history?.querySelectorAll('.vs-spark').length ?? 0,
        };
      } finally {
        screen.unmount();
      }
    };
    // A history that fits a screenful is drawn whole.
    const few = shownOf(8);
    expect(few.drawn).toBe(8 - LIVE);
    expect(few.setsize).toBe(String(8 - LIVE));
    // Histories well past a screenful, an order of magnitude apart: each names its own size, and only
    // the rows it draws draw their series.
    const many = [20, 60, 150].map(runs => ({runs, ...shownOf(runs)}));
    for (const {runs, drawn, setsize, sparks} of many) {
      expect(drawn).toBeGreaterThan(0);
      expect(drawn).toBeLessThan(60);
      expect(setsize).toBe(String(runs - LIVE));
      expect(sparks).toBeGreaterThan(0);
      expect(sparks).toBeLessThanOrEqual(drawn);
    }
    // However many runs there are, the same screenful is drawn: the window is bounded, never a
    // fraction of the history that grows with it.
    expect(new Set(many.map(each => each.drawn)).size).toBe(1);
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

/** One live host whose runs each want a look for one reason, a calm one, and two that are over. */
const troubled: FleetHostInput = withHost([
  live('calm', 'calm run', {reportMs: NOW - MINUTE, deadlineMs: NOW + 4 * HOUR}),
  live('final', 'final run', {finalReported: true, startedMs: NOW - HOUR, reportMs: NOW - MINUTE}),
  live('limit', 'limit run', {
    limit: RunLimit.COST,
    limitMs: NOW - 30 * MINUTE,
    cost: 510,
    budget: 500,
  }),
  live('stuck', 'stuck run', {
    search: create(SearchStateSchema, {
      runId: 'stuck',
      revision: 1n,
      troubles: [1, 2, 3].map(index => ({jobId: `job-${index}`, failure: {message: 'No worker'}})),
    }),
  }),
  // Told to finish at its time limit, the lead did nothing more.
  live('stalled', 'stalled run', {
    stalledMs: NOW - 2 * HOUR,
    limit: RunLimit.TIME,
    limitMs: NOW - 3 * HOUR,
  }),
  {
    id: 'kept',
    task: 'kept run',
    status: RunStatus.ENDED,
    startedMs: NOW - 3 * HOUR,
    endedMs: NOW - 2 * HOUR,
    kept: 2,
  },
  {
    id: 'done',
    task: 'done run',
    status: RunStatus.ENDED,
    startedMs: NOW - 3 * HOUR,
    endedMs: NOW - HOUR,
  },
]);

describe('the runs that want a look', () => {
  const statusOf = (screen: ReturnType<typeof mount>, name: string): HTMLElement =>
    cell(rowOf(screen, name), 'status');

  it('say why in the Status cell, the most pressing reason with how many more, in its tone', () => {
    const {screen} = open([troubled]);
    const note = (name: string) => statusOf(screen, name).querySelector('.vs-text');
    const said = (name: string) => [note(name)?.textContent, note(name)?.getAttribute('data-tone')];
    expect(said('stalled run')).toEqual(['Stalled 2h 0m +1', 'warn']);
    expect(said('stuck run')).toEqual(['3 jobs stuck', 'warn']);
    expect(said('limit run')).toEqual(['Cost limit reached', 'warn']);
    expect(said('final run')).toEqual(['Final report in', 'ok']);
    expect(said('kept run')).toEqual(['2 checkouts kept', 'warn']);
    // A run that wants no look says what it said before: the report's age, or when it ended.
    expect(statusOf(screen, 'calm run').textContent).toContain('Report 1m ago');
    expect(statusOf(screen, 'done run').textContent).toContain('Nov 7, 1:00 AM PST');
    // A run that wants a look says so to a screen reader too.
    expect(rowOf(screen, 'stuck run').getAttribute('aria-description')).toContain('3 jobs stuck');
    expect(rowOf(screen, 'calm run').getAttribute('aria-description')).not.toMatch(
      /stuck|Stalled|limit|kept/,
    );
    screen.unmount();
  });

  it('give every reason, with its account, on the pointer', () => {
    const {screen} = open([troubled]);
    const hint = (name: string) => statusOf(screen, name).getAttribute('title') ?? '';
    expect(hint('stalled run').split('\n')).toEqual([
      'Running, started Nov 6, 8:40 PM PST',
      'Stalled 2h 0m: Nothing has moved the run since Nov 7, 12:00 AM PST: no agent is working, no message waits and no job runs',
      'Time limit reached: The run reached its time limit at Nov 6, 11:00 PM PST; its lead was told to finish',
      'The lead has not posted a report',
    ]);
    expect(hint('limit run')).toContain(
      'Cost limit reached: The run reached its cost limit at Nov 7, 1:30 AM PST; its lead was told to finish',
    );
    expect(hint('stuck run')).toContain(
      '3 jobs stuck: 3 jobs are not finished and nothing is working on them',
    );
    expect(hint('kept run')).toContain(
      '2 checkouts kept: The run kept 2 working copies with changes git does not have',
    );
    screen.unmount();
  });

  it('tell how long a run that is over ran, and when a live one meets its time limit, on the pointer', () => {
    const {screen} = open([troubled]);
    expect(statusOf(screen, 'done run').querySelector('.vs-chip')?.textContent).toBe('Finished');
    expect(statusOf(screen, 'done run').getAttribute('title')).toBe(
      'Finished Nov 7, 1:00 AM PST, after 2h 0m',
    );
    expect(statusOf(screen, 'calm run').getAttribute('title')).toContain(
      'Time limit Nov 7, 6:00 AM PST',
    );
    screen.unmount();
  });

  it('name a reason before saying the host could not serve the report, which stays on the pointer', () => {
    const summary = troubled.data.summaries['stuck'];
    if (summary === undefined) throw new Error('the fixture serves a summary of stuck');
    const {screen} = open([
      {
        ...troubled,
        data: {
          ...troubled.data,
          summaries: {...troubled.data.summaries, stuck: {...summary, reportUnavailable: true}},
        },
      },
    ]);
    expect(statusOf(screen, 'stuck run').querySelector('.vs-text')?.textContent).toBe(
      '3 jobs stuck',
    );
    expect(statusOf(screen, 'stuck run').getAttribute('title')).toContain(
      'Report unavailable: the host could not serve it',
    );
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

  it('marks a run with nothing measured as unavailable', () => {
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
    expect(trend.textContent).toBe('—');
    screen.unmount();
  });

  it('marks a missing host series as unavailable, though it shows the best and the bound', () => {
    const {screen} = open([hostA]);
    const row = rowOf(screen, 'queue-rs');
    expect(cell(row, 'best').textContent).toBe('2.31k tok/s · 71%');
    expect(cell(row, 'trend').querySelector('svg')).toBeNull();
    expect(cell(row, 'trend').textContent).toBe('—');
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
  it('says how many older runs lack managed details, and nothing when every run has them', () => {
    const cut = (runsLeftOut: number): FleetHostInput => ({
      ...hostA,
      data: {...hostA.data, cut: {truncated: runsLeftOut > 0, runsLeftOut}},
    });
    const some = open([cut(7)]);
    expect(some.screen.text()).toContain('7 older runs lack managed details');
    some.screen.unmount();
    const none = open([cut(0)]);
    // Its room is kept, empty and hidden, so the chip never moves what is under it.
    expect(none.screen.container.querySelector('.vs-reserve [class~="vs-chip"]')).not.toBeNull();
    const reserves = [...none.screen.container.querySelectorAll('.vs-reserve')];
    expect(reserves).toHaveLength(2);
    for (const reserve of reserves) {
      expect(reserve.getAttribute('aria-hidden')).toBe('true');
      expect(reserve.getAttribute('data-collapse-narrow')).toBe('true');
    }
    expect(
      [...none.screen.container.querySelectorAll('.vs-chip')].filter(
        chip =>
          chip.closest('.vs-reserve') === null &&
          chip.textContent?.includes('lack managed details'),
      ),
    ).toHaveLength(0);
    none.screen.unmount();
    // Two hosts: the figure is of the hosts shown, and the host filter narrows it.
    const both = open([
      cut(7),
      {...hostB, data: {...hostB.data, cut: {truncated: true, runsLeftOut: 5}}},
    ]);
    expect(both.screen.text()).toContain('12 older runs lack managed details');
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
    const both = bar([hostA, {...hostB, freshness: 'live', status: statusFor('live', null, null)}]);
    expect(
      bar([
        hostA,
        {...hostB, freshness: 'empty', status: statusFor('empty', null, null), data: emptyState},
      ]),
    ).toHaveLength(both.length);
    expect(bar([hostA, hostB])).toHaveLength(both.length);
    expect(
      bar([
        hostA,
        {
          ...hostB,
          freshness: 'live',
          status: statusFor('live', null, null),
          data: {...hostB.data, cut: {truncated: true, runsLeftOut: 4}},
        },
      ]),
    ).toHaveLength(both.length);
  });

  it('says in the status cell, as a chip, that the host could not serve the report, and not that there is none', () => {
    const held = hostA.data;
    const summary = held.summaries['a1'];
    if (summary === undefined) throw new Error('the fixture serves a summary of a1');
    const unavailable = (flag: boolean): FleetHostInput => ({
      ...hostA,
      data: {
        ...held,
        summaries: {...held.summaries, a1: {...summary, reportUnavailable: flag}},
      },
    });
    const on = open([unavailable(true)]);
    const status = cell(rowOf(on.screen, 'queue-rs'), 'status');
    expect(status.querySelector('.vs-chip[data-tone="warn"]')?.textContent).toBe(
      'Report unavailable',
    );
    expect(status.textContent).not.toContain('No report');
    on.screen.unmount();
    const off = open([unavailable(false)]);
    expect(cell(rowOf(off.screen, 'queue-rs'), 'status').textContent).not.toContain('unavailable');
    off.screen.unmount();
  });
});
