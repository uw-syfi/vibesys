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
  type Run,
  RunSchema,
  RunStatus,
  SearchStateSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {Freshness} from '../freshness.js';
import {
  FLEET_STATES,
  type FleetFilter,
  type FleetHostInput,
  type FleetRow,
  fleetRows,
  fleetView,
  headObjectives,
  isLive,
  NO_FLEET_FILTER,
} from './fleet.js';
import {emptyState} from './state.js';

const STATUSES = [
  RunStatus.STARTING,
  RunStatus.RUNNING,
  RunStatus.STOPPING,
  RunStatus.ENDED,
  RunStatus.FAILED,
] as const;
const FRESHNESS: readonly Freshness[] = ['live', 'catching-up', 'stale', 'empty', 'unavailable'];

interface RunSpec {
  readonly id: string;
  readonly status: RunStatus;
  readonly startedMs: number;
  readonly endedMs: number | undefined;
  readonly neverOpened: boolean;
  readonly cost: number | undefined;
  readonly budget: number | undefined;
  readonly agentStatuses: readonly AgentStatus[];
  readonly reports: readonly number[];
  readonly otherMessages: readonly number[];
}

const runSpec: fc.Arbitrary<RunSpec> = fc.record({
  id: fc.constantFrom('r1', 'r2', 'r3', 'r4', 'r5', 'r6'),
  status: fc.constantFrom(...STATUSES),
  // Few distinct times, so that ties are common and the tiebreak is exercised.
  startedMs: fc.integer({min: 1, max: 6}).map(n => n * 60_000),
  endedMs: fc.option(fc.integer({min: 7, max: 12}), {nil: undefined}).map(n => n && n * 60_000),
  neverOpened: fc.boolean(),
  cost: fc.option(fc.double({min: 0, max: 5000, noNaN: true}), {nil: undefined}),
  budget: fc.option(fc.double({min: 1, max: 9000, noNaN: true}), {nil: undefined}),
  agentStatuses: fc.array(
    fc.constantFrom(
      AgentStatus.LAUNCHING,
      AgentStatus.ACTIVE,
      AgentStatus.WAITING,
      AgentStatus.IDLE,
      AgentStatus.ENDED,
      AgentStatus.FAILED,
      AgentStatus.LOST,
    ),
    {maxLength: 5},
  ),
  reports: fc.array(
    fc.integer({min: 1, max: 20}).map(n => n * 1000),
    {maxLength: 3},
  ),
  otherMessages: fc.array(
    fc.integer({min: 1, max: 20}).map(n => n * 1000),
    {maxLength: 3},
  ),
});

function dataOf(specs: readonly RunSpec[]) {
  // One run per id, as a host keeps one record per id.
  const unique = [...new Map(specs.map(spec => [spec.id, spec])).values()];
  const runs: Run[] = unique.map(spec =>
    create(RunSchema, {
      id: spec.id,
      task: `task-${spec.id}`,
      repository: 'repo',
      status: spec.status,
      createdAt: timestampFromMs(spec.startedMs),
      ...(spec.endedMs === undefined ? {} : {endedAt: timestampFromMs(spec.endedMs)}),
      neverOpened: spec.neverOpened,
      ...(spec.cost === undefined ? {} : {usage: {costUsd: spec.cost}}),
      ...(spec.budget === undefined ? {} : {maxCostUsd: spec.budget}),
    }),
  );
  const agents = unique.flatMap(spec =>
    spec.agentStatuses.map((status, index) =>
      create(AgentSchema, {
        id: `${spec.id}-a${index}`,
        runId: spec.id,
        role: AgentRole.WORKER,
        harness: Harness.CODEX,
        status,
      }),
    ),
  );
  const messages = unique.flatMap(spec => [
    ...spec.reports.map((at, index) =>
      create(MessageSchema, {
        id: `${spec.id}-report-${index}`,
        runId: spec.id,
        at: timestampFromMs(at),
        body: {
          case: 'report',
          value: create(ReportSchema, {headline: 'h', kind: ReportKind.PROGRESS}),
        },
      }),
    ),
    ...spec.otherMessages.map((at, index) =>
      create(MessageSchema, {
        id: `${spec.id}-text-${index}`,
        runId: spec.id,
        at: timestampFromMs(at),
        body: {case: 'text', value: 'hello'},
      }),
    ),
  ]);
  return {
    ...emptyState,
    runs: Object.fromEntries(runs.map(run => [run.id, run])),
    agents: Object.fromEntries(agents.map(agent => [agent.id, agent])),
    messages: Object.fromEntries(messages.map(message => [message.id, message])),
  };
}

const hostInput: fc.Arbitrary<{
  readonly freshness: Freshness;
  readonly staleForMs: number | null;
  readonly specs: readonly RunSpec[];
}> = fc.record({
  freshness: fc.constantFrom(...FRESHNESS),
  staleForMs: fc.option(fc.integer({min: 0, max: 10_000_000}), {nil: null}),
  specs: fc.array(runSpec, {maxLength: 8}),
});

/** One to four hosts with distinct keys; the same run ids repeat across them. */
const fleet: fc.Arbitrary<readonly FleetHostInput[]> = fc
  .array(hostInput, {minLength: 1, maxLength: 4})
  .map(inputs =>
    inputs.map((input, index) => ({
      key: `host-${index}`,
      label: `Host ${index}`,
      data: dataOf(input.specs),
      freshness: input.freshness,
      staleForMs: input.staleForMs,
    })),
  );

const filter: fc.Arbitrary<FleetFilter> = fc.record({
  state: fc.constantFrom(...FLEET_STATES),
  host: fc.option(fc.constantFrom('host-0', 'host-1', 'host-2', 'host-3', 'nobody'), {nil: null}),
});

const keysOf = (rows: readonly FleetRow[]): string[] => rows.map(row => row.key);
const sorted = (keys: readonly string[]): string[] => [...keys].sort();
const shown = (hosts: readonly FleetHostInput[], by: FleetFilter): FleetRow[] => {
  const view = fleetView(hosts, by);
  return [...view.live, ...view.history];
};

describe('the fleet of runs', () => {
  it('has one row for each run of each host, with a key that is unique across hosts', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        const expected = hosts.flatMap(host =>
          Object.values(host.data.runs)
            .filter(run => !run.neverOpened)
            .map(run => `${host.key}/${run.id}`),
        );
        const rows = shown(hosts, NO_FLEET_FILTER);
        expect(sorted(keysOf(rows))).toEqual(sorted(expected));
        expect(new Set(keysOf(rows)).size).toBe(rows.length);
      }),
    );
  });

  it('leaves out a run that failed before its lead ever ran, and only that', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        const kept = new Set(keysOf(fleetRows(hosts)));
        for (const host of hosts) {
          for (const run of Object.values(host.data.runs))
            expect(kept.has(`${host.key}/${run.id}`)).toBe(!run.neverOpened);
        }
      }),
    );
  });

  it('puts live runs first, newest start first, then history, newest end first, in a total order', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        const view = fleetView(hosts, NO_FLEET_FILTER);
        expect(view.live.every(row => row.live && isLive(row.status))).toBe(true);
        expect(view.history.every(row => !row.live && !isLive(row.status))).toBe(true);
        const ordered = (rows: readonly FleetRow[], time: (row: FleetRow) => number) => {
          for (const [index, row] of rows.entries()) {
            const next = rows[index + 1];
            if (next === undefined) continue;
            const [a, b] = [time(row), time(next)];
            expect(a > b || (a === b && row.key < next.key)).toBe(true);
          }
        };
        ordered(view.live, row => row.startedMs ?? Number.NEGATIVE_INFINITY);
        ordered(view.history, row => row.endedMs ?? row.startedMs ?? Number.NEGATIVE_INFINITY);
      }),
    );
  });

  it('shows the same rows in the same order whatever order the hosts are listed in', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        const reversed = [...hosts].reverse();
        const flat = (view: ReturnType<typeof fleetView>) => [
          ...keysOf(view.live),
          ...keysOf(view.history),
        ];
        expect(flat(fleetView(reversed, NO_FLEET_FILTER))).toEqual(
          flat(fleetView(hosts, NO_FLEET_FILTER)),
        );
      }),
    );
  });

  it('filters by state and by host exactly as a person would: a subset of all, nothing invented', () => {
    fc.assert(
      fc.property(fleet, filter, (hosts, by) => {
        const all = shown(hosts, NO_FLEET_FILTER);
        const rows = shown(hosts, by);
        const expected = all.filter(
          row =>
            (by.state === 'all' || (by.state === 'live') === row.live) &&
            (by.host === null || row.host.key === by.host),
        );
        expect(keysOf(rows)).toEqual(keysOf(expected));
        // A filtered row is the very row the unfiltered list has.
        for (const row of rows) expect(all.find(each => each.key === row.key)).toEqual(row);
      }),
    );
  });

  it('lists every host in the order given, with the runs it would show under the state filter', () => {
    fc.assert(
      fc.property(fleet, filter, (hosts, by) => {
        const view = fleetView(hosts, by);
        expect(view.hosts.map(host => host.key)).toEqual(hosts.map(host => host.key));
        const underState = shown(hosts, {state: by.state, host: null});
        for (const host of view.hosts) {
          expect(host.runs).toBe(underState.filter(row => row.host.key === host.key).length);
        }
        // The host filter never changes what the strip says about the other hosts.
        const whole = fleetView(hosts, {state: by.state, host: null});
        expect(view.hosts).toEqual(whole.hosts);
      }),
    );
  });

  it('keeps the rows of a host that is stale or cannot be reached, each carrying its host freshness', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        for (const row of shown(hosts, NO_FLEET_FILTER)) {
          const host = hosts.find(each => each.key === row.host.key);
          expect(row.host.freshness).toBe(host?.freshness);
          expect(row.host.staleForMs).toBe(host?.staleForMs);
        }
        // Whatever the freshness, nothing is dropped for it.
        const live = hosts.map(host => ({...host, freshness: 'live' as const}));
        expect(keysOf(shown(hosts, NO_FLEET_FILTER))).toEqual(keysOf(shown(live, NO_FLEET_FILTER)));
      }),
    );
  });

  it('counts the agents that are starting, working or waiting, and no others', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        for (const row of shown(hosts, NO_FLEET_FILTER)) {
          const host = hosts.find(each => each.key === row.host.key);
          const expected = Object.values(host?.data.agents ?? {}).filter(
            agent =>
              agent.runId === row.runId &&
              [AgentStatus.LAUNCHING, AgentStatus.ACTIVE, AgentStatus.WAITING].includes(
                agent.status,
              ),
          ).length;
          expect(row.agentsRunning).toBe(expected);
        }
      }),
    );
  });

  it('dates the last report from reports only, never another kind of message', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        for (const row of shown(hosts, NO_FLEET_FILTER)) {
          const host = hosts.find(each => each.key === row.host.key);
          const reports = Object.values(host?.data.messages ?? {})
            .filter(
              message =>
                message.runId === row.runId && message.id.startsWith(`${row.runId}-report-`),
            )
            .map(message => Number(message.at?.seconds ?? 0n) * 1000);
          expect(row.lastReportMs).toBe(reports.length === 0 ? null : Math.max(...reports));
        }
      }),
    );
  });

  it('shows cost and budget as the host sent them, absent when it sent none, never zero', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        for (const row of shown(hosts, NO_FLEET_FILTER)) {
          const host = hosts.find(each => each.key === row.host.key);
          const run = host?.data.runs[row.runId];
          expect(row.costUsd).toBe(run?.usage?.costUsd ?? null);
          expect(row.budgetUsd).toBe(run?.maxCostUsd ?? null);
        }
      }),
    );
  });

  it('is total on a host that has said nothing yet: no rows, and the host still listed', () => {
    const view = fleetView(
      [{key: 'a', label: 'A', data: emptyState, freshness: 'empty', staleForMs: null}],
      NO_FLEET_FILTER,
    );
    expect(view.live).toEqual([]);
    expect(view.history).toEqual([]);
    expect(view.hosts).toEqual([
      {key: 'a', label: 'A', freshness: 'empty', staleForMs: null, runs: 0},
    ]);
  });
});

const measuredInit = (fields: {
  readonly scored: string;
  readonly value: number;
  readonly others?: readonly {readonly name: string; readonly value: number | undefined}[];
}) => ({
  head: {
    state: {
      case: 'current' as const,
      value: {
        head: 'b'.repeat(40),
        result: {
          outcome: {
            case: 'measure' as const,
            value: {
              metric: {name: fields.scored},
              value: fields.value,
              ...(fields.others === undefined
                ? {}
                : {
                    objectives: {
                      metrics: [fields.scored, ...fields.others.map(other => other.name)].map(
                        name => ({name}),
                      ),
                    },
                    observations: fields.others.map(other => ({
                      name: other.name,
                      ...(other.value === undefined ? {} : {value: other.value}),
                    })),
                  }),
            },
          },
        },
      },
    },
  },
});

const measured = (fields: Parameters<typeof measuredInit>[0]) =>
  create(SearchStateSchema, measuredInit(fields));

const bound = (
  objective: string,
  fields: {readonly headValue?: number; readonly percentOfBound?: number; readonly unit?: string},
) => ({
  objective,
  direction: Direction.MAXIMIZE,
  versions: [
    {
      seq: 1n,
      objective,
      direction: Direction.MAXIMIZE,
      value: 100,
      unit: fields.unit ?? 'tok/s',
      note: '',
    },
  ],
  ...(fields.headValue === undefined ? {} : {headValue: fields.headValue}),
  ...(fields.percentOfBound === undefined ? {} : {percentOfBound: fields.percentOfBound}),
});

describe('the best result of a run, per objective', () => {
  it('is nothing for a run with no search state or no head', () => {
    expect(headObjectives(undefined)).toEqual([]);
    expect(headObjectives(create(SearchStateSchema))).toEqual([]);
  });

  it('is the head value of the scored metric, with no unit and no percent until bounds are served', () => {
    expect(headObjectives(measured({scored: 'Throughput', value: 42}))).toEqual([
      {name: 'Throughput', value: 42, unit: '', percentOfBound: null},
    ]);
  });

  it('names every objective of a multi-objective measurement, the scored one and the observed ones', () => {
    expect(
      headObjectives(
        measured({
          scored: 'tok_s',
          value: 2300,
          others: [
            {name: 'ttft_ms', value: 38},
            {name: 'mem_gb', value: undefined},
          ],
        }),
      ).map(({name, value}) => [name, value]),
    ).toEqual([
      ['tok_s', 2300],
      ['ttft_ms', 38],
      ['mem_gb', null],
    ]);
  });

  it('shows the percent of the bound exactly as the host served it, and none when it served none', () => {
    fc.assert(
      fc.property(
        fc.option(fc.double({min: 0.01, max: 500, noNaN: true}), {nil: undefined}),
        fc.double({min: 1, max: 1e6, noNaN: true}),
        (percentOfBound, value) => {
          const search = create(SearchStateSchema, {
            ...measuredInit({scored: 'tok_s', value}),
            bounds: [
              bound('tok_s', {
                headValue: value,
                unit: 'tok/s',
                ...(percentOfBound === undefined ? {} : {percentOfBound}),
              }),
            ],
          });
          const [objective] = headObjectives(search);
          // The app divides nothing: with no served percent there is none, whatever the numbers give.
          expect(objective?.percentOfBound).toBe(percentOfBound ?? null);
          expect(objective?.unit).toBe('tok/s');
          expect(objective?.value).toBe(value);
        },
      ),
    );
  });

  it('keeps a declared bound whose objective the head has not measured, with the value the host served', () => {
    const search = create(SearchStateSchema, {
      ...measuredInit({scored: 'tok_s', value: 10}),
      bounds: [bound('mem_gb', {unit: 'GB'}), bound('tok_s', {percentOfBound: 40})],
    });
    expect(headObjectives(search)).toEqual([
      {name: 'tok_s', value: 10, unit: 'tok/s', percentOfBound: 40},
      {name: 'mem_gb', value: null, unit: 'GB', percentOfBound: null},
    ]);
  });
});
