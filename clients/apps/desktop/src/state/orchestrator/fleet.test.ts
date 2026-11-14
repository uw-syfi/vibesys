import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  Harness,
  MessageSchema,
  ObjectiveStandingSchema,
  ReportKind,
  ReportSchema,
  ReportScope,
  type Run,
  RunLimit,
  RunSchema,
  RunStatus,
  type RunSummary,
  RunSummarySchema,
  SearchStateSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {Freshness} from '../freshness.js';
import {
  DORMANT_GENERIC_RUN_INDEX,
  type GenericRunEntry,
  type GenericRunIndex,
} from '../generic-run-index.js';
import {HOST_STATUS_KINDS, type HostStatus} from '../host-status.js';
import {
  ATTENTION_KINDS,
  FLEET_STATES,
  type FleetAttention,
  type FleetFilter,
  type FleetHostInput,
  type FleetHostRef,
  type FleetRow,
  fleetOverview,
  fleetRows,
  fleetView,
  hostFleetOverview,
  isLive,
  NO_FLEET_FILTER,
} from './fleet.js';
import {emptyState, type OrchestratorState} from './state.js';

/** A whole-second time as milliseconds; the specs below set no fraction of a second. */
const secondsMs = (at: {readonly seconds: bigint} | undefined): number | null =>
  at === undefined ? null : Number(at.seconds) * 1000;

/**
 * Why a run wants a look, read straight off the facts the host keeps of it: a live run for its
 * stall, its stuck jobs, its limit and its final report; a run that is over only for the checkouts
 * it kept.
 */
function reasonsOf(run: Run, troubles: number): FleetAttention[] {
  const kept = run.keptCheckouts.length;
  if (!isLive(run.status)) return kept > 0 ? [{kind: 'kept', checkouts: kept}] : [];
  const sinceMs = secondsMs(run.stalledSince);
  const limit = {
    [RunLimit.UNSPECIFIED]: null,
    [RunLimit.TIME]: 'time',
    [RunLimit.COST]: 'cost',
  } as const;
  const reached = limit[run.limitReached];
  return [
    ...(sinceMs === null ? [] : [{kind: 'stalled', sinceMs} as const]),
    ...(troubles === 0 ? [] : [{kind: 'troubles', jobs: troubles} as const]),
    ...(reached === null
      ? []
      : [{kind: 'limit', limit: reached, atMs: secondsMs(run.limitReachedAt)} as const]),
    ...(run.finalReported ? [{kind: 'final'} as const] : []),
  ];
}

const STATUSES = [
  RunStatus.STARTING,
  RunStatus.RUNNING,
  RunStatus.STOPPING,
  RunStatus.ENDED,
  RunStatus.FAILED,
] as const;
const FRESHNESS: readonly Freshness[] = ['live', 'catching-up', 'stale', 'empty', 'unavailable'];

/** The summary the host served of a run: what the fleet shows of it, and nothing else. */
interface SummarySpec {
  readonly working: number;
  readonly reportAtMs: number | undefined;
  readonly objectives: readonly {
    readonly name: string;
    readonly value: number | undefined;
    readonly unit: string;
    readonly percent: number | undefined;
  }[];
}

/**
 * What is held of a run besides its summary: agents and messages of any kind and scope, which the
 * fleet must not read. They are noise to it, and any amount of them leaves its rows as they were.
 */
interface NoiseSpec {
  readonly agentStatuses: readonly AgentStatus[];
  readonly messages: readonly {readonly atMs: number; readonly scope: ReportScope}[];
}

interface RunSpec {
  readonly id: string;
  readonly status: RunStatus;
  readonly startedMs: number;
  readonly endedMs: number | undefined;
  readonly neverOpened: boolean;
  readonly cost: number | undefined;
  readonly budget: number | undefined;
  readonly summary: SummarySpec | undefined;
  readonly noise: NoiseSpec;
  /** The facts the host keeps of why the run may want a look, each set or not at random. */
  readonly stalledMs: number | undefined;
  readonly troubles: number;
  readonly limit: RunLimit;
  readonly limitMs: number | undefined;
  readonly finalReported: boolean;
  readonly kept: number;
  readonly deadlineMs: number | undefined;
}

const summarySpec: fc.Arbitrary<SummarySpec> = fc.record({
  working: fc.integer({min: 0, max: 9}),
  reportAtMs: fc.option(
    fc.integer({min: 1, max: 20}).map(n => n * 1000),
    {nil: undefined},
  ),
  objectives: fc.array(
    fc.record({
      name: fc.constantFrom('tput', 'latency', 'memory'),
      value: fc.option(fc.double({min: 0.5, max: 1e6, noNaN: true}), {nil: undefined}),
      unit: fc.constantFrom('', 'ms', 'tok/s'),
      percent: fc.option(fc.double({min: 0.01, max: 500, noNaN: true}), {nil: undefined}),
    }),
    {maxLength: 3},
  ),
});

const noiseSpec: fc.Arbitrary<NoiseSpec> = fc.record({
  agentStatuses: fc.array(
    fc.constantFrom(
      AgentStatus.LAUNCHING,
      AgentStatus.ACTIVE,
      AgentStatus.WAITING,
      AgentStatus.IDLE,
      AgentStatus.ENDED,
    ),
    {maxLength: 5},
  ),
  messages: fc.array(
    fc.record({
      atMs: fc.integer({min: 1, max: 40}).map(n => n * 1000),
      scope: fc.constantFrom(ReportScope.UNSPECIFIED, ReportScope.RUN, ReportScope.WINDOW),
    }),
    {maxLength: 4},
  ),
});

const runSpec: fc.Arbitrary<RunSpec> = fc.record({
  id: fc.constantFrom('r1', 'r2', 'r3', 'r4', 'r5', 'r6'),
  status: fc.constantFrom(...STATUSES),
  // Few distinct times, so that ties are common and the tiebreak is exercised.
  startedMs: fc.integer({min: 1, max: 6}).map(n => n * 60_000),
  endedMs: fc.option(fc.integer({min: 7, max: 12}), {nil: undefined}).map(n => n && n * 60_000),
  neverOpened: fc.boolean(),
  cost: fc.option(fc.double({min: 0, max: 5000, noNaN: true}), {nil: undefined}),
  budget: fc.option(fc.double({min: 1, max: 9000, noNaN: true}), {nil: undefined}),
  summary: fc.option(summarySpec, {nil: undefined}),
  noise: noiseSpec,
  stalledMs: fc.option(
    fc.integer({min: 1, max: 40}).map(n => n * 1000),
    {nil: undefined},
  ),
  troubles: fc.integer({min: 0, max: 3}),
  limit: fc.constantFrom(RunLimit.UNSPECIFIED, RunLimit.TIME, RunLimit.COST),
  limitMs: fc.option(
    fc.integer({min: 1, max: 40}).map(n => n * 1000),
    {nil: undefined},
  ),
  finalReported: fc.boolean(),
  kept: fc.integer({min: 0, max: 3}),
  deadlineMs: fc.option(
    fc.integer({min: 1, max: 40}).map(n => n * 60_000),
    {nil: undefined},
  ),
});

function summaryOf(runId: string, spec: SummarySpec): RunSummary {
  return create(RunSummarySchema, {
    runId,
    agentsWorking: spec.working,
    agentsTotal: spec.working + 2,
    ...(spec.reportAtMs === undefined
      ? {}
      : {
          report: create(MessageSchema, {
            id: `${runId}-summary-report`,
            runId,
            at: timestampFromMs(spec.reportAtMs),
            body: {
              case: 'report',
              value: create(ReportSchema, {
                headline: 'h',
                kind: ReportKind.PROGRESS,
                scope: ReportScope.RUN,
              }),
            },
          }),
        }),
    objectives: spec.objectives.map(objective =>
      create(ObjectiveStandingSchema, {
        name: objective.name,
        unit: objective.unit,
        // The head and the best are two things, and a row shows the best.
        ...(objective.value === undefined
          ? {}
          : {bestValue: objective.value, headValue: objective.value / 2}),
        ...(objective.percent === undefined
          ? {}
          : {bestPercentOfBound: objective.percent, headPercentOfBound: objective.percent / 2}),
      }),
    ),
  });
}

/** The state of a host with these runs; `noise` says whether the agents and messages are held at all. */
function dataOf(specs: readonly RunSpec[], noise = true): OrchestratorState {
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
      ...(spec.stalledMs === undefined ? {} : {stalledSince: timestampFromMs(spec.stalledMs)}),
      limitReached: spec.limit,
      ...(spec.limitMs === undefined ? {} : {limitReachedAt: timestampFromMs(spec.limitMs)}),
      finalReported: spec.finalReported,
      keptCheckouts: Array.from({length: spec.kept}, (_, index) => ({agentId: `w${index}`})),
      ...(spec.deadlineMs === undefined ? {} : {deadline: timestampFromMs(spec.deadlineMs)}),
    }),
  );
  const agents = noise
    ? unique.flatMap(spec =>
        spec.noise.agentStatuses.map((status, index) =>
          create(AgentSchema, {
            id: `${spec.id}-a${index}`,
            runId: spec.id,
            role: AgentRole.WORKER,
            harness: Harness.CODEX,
            status,
          }),
        ),
      )
    : [];
  const messages = noise
    ? unique.flatMap(spec =>
        spec.noise.messages.map(({atMs, scope}, index) =>
          create(MessageSchema, {
            id: `${spec.id}-report-${index}`,
            runId: spec.id,
            at: timestampFromMs(atMs),
            body: {
              case: 'report',
              value: create(ReportSchema, {headline: 'h', kind: ReportKind.PROGRESS, scope}),
            },
          }),
        ),
      )
    : [];
  return {
    ...emptyState,
    runs: Object.fromEntries(runs.map(run => [run.id, run])),
    summaries: Object.fromEntries(
      unique.flatMap(spec =>
        spec.summary === undefined ? [] : [[spec.id, summaryOf(spec.id, spec.summary)] as const],
      ),
    ),
    agents: Object.fromEntries(agents.map(agent => [agent.id, agent])),
    messages: Object.fromEntries(messages.map(message => [message.id, message])),
    searches: Object.fromEntries(
      unique.flatMap(spec =>
        spec.troubles === 0
          ? []
          : [
              [
                spec.id,
                create(SearchStateSchema, {
                  runId: spec.id,
                  revision: 1n,
                  troubles: Array.from({length: spec.troubles}, (_, index) => ({
                    jobId: `${spec.id}-job-${index}`,
                    failure: {message: 'No worker'},
                  })),
                }),
              ] as const,
            ],
      ),
    ),
  };
}

const hostInput: fc.Arbitrary<{
  readonly freshness: Freshness;
  readonly status: HostStatus;
  readonly specs: readonly RunSpec[];
  readonly leftOut: number;
}> = fc.record({
  freshness: fc.constantFrom(...FRESHNESS),
  status: fc.record({
    kind: fc.constantFrom(...HOST_STATUS_KINDS),
    cause: fc.constantFrom(null, 'job-tool-too-old' as const),
    staleForMs: fc.option(fc.integer({min: 0, max: 10_000_000}), {nil: null}),
  }),
  specs: fc.array(runSpec, {maxLength: 8}),
  leftOut: fc.integer({min: 0, max: 40}),
});

/** One to four hosts with distinct keys; the same run ids repeat across them. */
const fleet: fc.Arbitrary<readonly FleetHostInput[]> = fc
  .array(hostInput, {minLength: 1, maxLength: 4})
  .map(inputs =>
    inputs.map((input, index) => ({
      key: `host-${index}`,
      label: `Host ${index}`,
      data: {
        ...dataOf(input.specs),
        cut: {truncated: input.leftOut > 0, runsLeftOut: input.leftOut},
      },
      freshness: input.freshness,
      status: input.status,
      runIndex: DORMANT_GENERIC_RUN_INDEX,
    })),
  );

const filter: fc.Arbitrary<FleetFilter> = fc.record({
  state: fc.constantFrom(...FLEET_STATES),
  host: fc.option(fc.constantFrom('host-0', 'host-1', 'host-2', 'host-3', 'nobody'), {nil: null}),
  query: fc.constant(''),
});

const keysOf = (rows: readonly FleetRow[]): string[] => rows.map(row => row.key);
const sorted = (keys: readonly string[]): string[] => [...keys].sort();
const shown = (hosts: readonly FleetHostInput[], by: FleetFilter): FleetRow[] => {
  const view = fleetView(hosts, by);
  return [...view.live, ...view.history, ...view.unknown];
};

/** What a run's search view says it started and tried, null for both when no view was read. */
const searchCounts = (
  search: OrchestratorState['searches'][string] | undefined,
  runId: string,
): [number | null, number | null] =>
  search === undefined
    ? [null, null]
    : [
        search.workstreams.filter(each => each.origin?.runId === runId).length,
        search.ideas.filter(each => each.inRun).length,
      ];

const genericRun = (
  runId: string,
  state: GenericRunEntry['state'] = 'running',
  activity: GenericRunEntry['activity'] = 'live',
): GenericRunEntry => ({
  runId,
  projectRoot: `/projects/${runId}`,
  state,
  activity,
  objective: {name: 'Throughput', unit: 'requests/s', bestValue: 42},
  costUsd: 1.25,
  needsUser: false,
});

const readyIndex = (
  runs: readonly GenericRunEntry[],
  freshness: 'current' | 'stale' = 'current',
): GenericRunIndex => ({
  _tag: 'Ready',
  freshness,
  runs: new Map(runs.map(run => [run.runId, run])),
});

function mixedHost(managedIds: readonly string[], generic: readonly GenericRunEntry[]) {
  return {
    key: 'host-a',
    label: 'Host A',
    data: {
      ...emptyState,
      runs: Object.fromEntries(
        managedIds.map(runId => [
          runId,
          create(RunSchema, {
            id: runId,
            task: `Managed ${runId}`,
            repository: 'repository',
            status: RunStatus.RUNNING,
          }),
        ]),
      ),
    },
    runIndex: readyIndex(generic),
    freshness: 'live' as const,
    status: {kind: 'live' as const, cause: null, staleForMs: null},
  };
}

describe('the fleet of runs', () => {
  it('proves an empty fleet only when both discovery streams are current and complete', () => {
    const host = mixedHost([], []);
    const cases = [
      {freshness: 'live', runIndex: readyIndex([]), expected: {kind: 'complete'}},
      {freshness: 'live', runIndex: DORMANT_GENERIC_RUN_INDEX, expected: {kind: 'opening'}},
      {freshness: 'empty', runIndex: readyIndex([]), expected: {kind: 'opening'}},
      {freshness: 'live', runIndex: readyIndex([], 'stale'), expected: {kind: 'unconfirmed'}},
      {freshness: 'stale', runIndex: readyIndex([]), expected: {kind: 'unconfirmed'}},
      {
        freshness: 'live',
        runIndex: {_tag: 'Unavailable' as const, reason: 'unsupported' as const},
        expected: {kind: 'unavailable', reason: 'unsupported'},
      },
      {
        freshness: 'unavailable',
        runIndex: readyIndex([]),
        expected: {kind: 'unavailable', reason: null},
      },
      {
        freshness: 'unavailable',
        runIndex: {_tag: 'Unavailable' as const, reason: 'transport' as const},
        expected: {kind: 'unavailable', reason: 'transport'},
      },
    ] as const;
    for (const {freshness, runIndex, expected} of cases) {
      const status =
        freshness === 'live'
          ? {kind: 'live' as const, cause: null, staleForMs: null}
          : freshness === 'empty'
            ? {kind: 'waiting' as const, cause: null, staleForMs: null}
            : freshness === 'stale'
              ? {kind: 'stale' as const, cause: null, staleForMs: 1}
              : {kind: 'unavailable' as const, cause: null, staleForMs: null};
      expect(fleetView([{...host, freshness, status, runIndex}], NO_FLEET_FILTER).absence).toEqual(
        expected,
      );
    }
  });

  it('uses only the selected hosts as absence evidence', () => {
    const current = {...mixedHost([], []), key: 'current'};
    const opening = {
      ...mixedHost([], []),
      key: 'opening',
      runIndex: DORMANT_GENERIC_RUN_INDEX,
    };
    expect(fleetView([current, opening], NO_FLEET_FILTER).absence).toEqual({kind: 'opening'});
    expect(fleetView([current, opening], {state: 'all', host: 'current'}).absence).toEqual({
      kind: 'complete',
    });
  });

  it('cannot prove a filtered section empty while a current run has unknown activity', () => {
    const unknown = genericRun('unknown', 'unknown', 'unknown');
    const host = mixedHost([], [unknown]);
    expect(fleetView([host], {state: 'live', host: null}).absence).toEqual({
      kind: 'unconfirmed',
    });
    expect(fleetView([host], {state: 'ended', host: null}).absence).toEqual({
      kind: 'unconfirmed',
    });
    expect(fleetView([host], NO_FLEET_FILTER).absence).toEqual({kind: 'complete'});
  });

  it('unions generic and managed discovery by host and run id, with the managed row authoritative', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.constantFrom('r1', 'r2', 'r3', 'r4', 'r5')),
        fc.uniqueArray(fc.constantFrom('r1', 'r2', 'r3', 'r4', 'r5')),
        (managedIds, genericIds) => {
          const rows = fleetRows([
            mixedHost(
              managedIds,
              genericIds.map(id => genericRun(id)),
            ),
          ]);
          const expected = new Set([...managedIds, ...genericIds]);
          const managed = new Set<string>(managedIds);
          expect(new Set(rows.map(row => row.runId))).toEqual(expected);
          expect(rows).toHaveLength(expected.size);
          for (const row of rows) {
            expect(row.source).toBe(managed.has(row.runId) ? 'managed' : 'generic');
          }
        },
      ),
      {seed: 49},
    );
  });

  it('keeps generic availability and unknown activity explicit instead of calling it ended', () => {
    const current = genericRun('current');
    const stale = {...genericRun('stale', 'finished', 'ended'), needsUser: true};
    const unknown = genericRun('unknown', 'unknown', 'unknown');
    const host = mixedHost([], [current, stale, unknown]);
    const view = fleetView(
      [{...host, runIndex: readyIndex([current, stale, unknown], 'stale')}],
      NO_FLEET_FILTER,
    );
    expect(view.live.map(row => row.runId)).toEqual(['current']);
    expect(view.history.map(row => row.runId)).toEqual(['stale']);
    expect(view.unknown.map(row => row.runId)).toEqual(['unknown']);
    const row = view.live[0];
    expect(row).toMatchObject({
      source: 'generic',
      repository: '/projects/current',
      indexFreshness: 'stale',
      costUsd: 1.25,
      objectives: [{name: 'Throughput', value: 42, unit: 'requests/s'}],
    });
    expect(shown([host], {state: 'ended', host: null}).map(each => each.runId)).toEqual(['stale']);
  });

  it('does not project a failed managed stream onto a current generic row', () => {
    const host = {
      ...mixedHost([], [genericRun('generic')]),
      freshness: 'unavailable' as const,
      status: {kind: 'unavailable' as const, cause: null, staleForMs: null},
    };
    const [row] = fleetRows([host]);
    expect(row).toMatchObject({source: 'generic', indexFreshness: 'current'});
    expect(row?.host.status).toEqual({kind: 'live', cause: null, staleForMs: null});
  });

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
        expect(
          view.live.every(row => row.source === 'managed' && row.live && isLive(row.status)),
        ).toBe(true);
        expect(
          view.history.every(row => row.source === 'managed' && !row.live && !isLive(row.status)),
        ).toBe(true);
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
        const underState = shown(hosts, {state: by.state, host: null, query: ''});
        for (const host of view.hosts) {
          expect(host.runs).toBe(underState.filter(row => row.host.key === host.key).length);
        }
        // The host filter never changes what the strip says about the other hosts.
        const whole = fleetView(hosts, {state: by.state, host: null, query: ''});
        expect(view.hosts).toEqual(whole.hosts);
      }),
    );
  });

  it('says for each host how many runs its snapshot left out, as the host said it', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        const view = fleetView(hosts, NO_FLEET_FILTER);
        for (const [index, host] of view.hosts.entries())
          expect(host.runsLeftOut).toBe(hosts[index]?.data.cut.runsLeftOut);
      }),
    );
  });

  it('keeps the rows of a host that is stale or cannot be reached, each carrying its host freshness', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        for (const row of shown(hosts, NO_FLEET_FILTER)) {
          const host = hosts.find(each => each.key === row.host.key);
          expect(row.host.freshness).toBe(host?.freshness);
          expect(row.host.status).toBe(host?.status);
        }
        // Whatever the freshness, nothing is dropped for it.
        const live = hosts.map(host => ({...host, freshness: 'live' as const}));
        expect(keysOf(shown(hosts, NO_FLEET_FILTER))).toEqual(keysOf(shown(live, NO_FLEET_FILTER)));
      }),
    );
  });

  it('shows the agents working and the time of the last report as the run summary says, absent when the host sent none', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        for (const row of shown(hosts, NO_FLEET_FILTER)) {
          const host = hosts.find(each => each.key === row.host.key);
          const summary = host?.data.summaries[row.runId];
          expect(row.agentsRunning).toBe(summary?.agentsWorking ?? null);
          const at = summary?.report?.at;
          expect(row.lastReportMs).toBe(
            at === undefined ? null : Number(at.seconds) * 1000 + Math.floor(at.nanos / 1e6),
          );
          // A row shows the best the run measured and its share of the bound, never the head's.
          expect(
            row.objectives.map(({name, value, unit, percentOfBound}) => ({
              name,
              value: value ?? undefined,
              unit,
              percentOfBound: percentOfBound ?? undefined,
            })),
          ).toEqual(
            (summary?.objectives ?? []).map(({name, bestValue, unit, bestPercentOfBound}) => ({
              name,
              value: bestValue,
              unit,
              percentOfBound: bestPercentOfBound,
            })),
          );
        }
      }),
    );
  });

  it('counts the workstreams and ideas of a run from its own search view, absent when none was read', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        for (const row of shown(hosts, NO_FLEET_FILTER)) {
          if (row.source !== 'managed') continue;
          const host = hosts.find(each => each.key === row.host.key);
          const search = host?.data.searches[row.runId];
          expect([row.workstreams, row.ideas]).toEqual(searchCounts(search, row.runId));
          const limit = host?.data.runs[row.runId]?.fanoutLimit ?? 0;
          expect(row.fanoutLimit).toBe(limit > 0 ? limit : null);
        }
      }),
    );
  });

  it('reads what one host carries from its model alone, as the overview reads the managed rows', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        for (const host of hosts) {
          const managed = fleetRows([host]).filter(row => row.source === 'managed');
          expect(hostFleetOverview(host.data)).toEqual(fleetOverview(managed));
        }
      }),
    );
  });

  // The fleet derives nothing: a host that held any agents and any messages (a window report among
  // them, which the host marks and the fleet never inspects) shows the rows of a host that held none.
  it('reads nothing but runs, their summaries and their searches: the agents and messages held change no row', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        const bare = hosts.map(host => ({
          ...host,
          data: {...host.data, agents: {}, messages: {}, turns: {}, checkIns: {}},
        }));
        expect(fleetView(hosts, NO_FLEET_FILTER)).toEqual(fleetView(bare, NO_FLEET_FILTER));
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

  it('says a live run wants a look exactly for each fact the host keeps of it, the most pressing first', () => {
    fc.assert(
      fc.property(fleet, hosts => {
        for (const row of shown(hosts, NO_FLEET_FILTER)) {
          const data = hosts.find(each => each.key === row.host.key)?.data;
          const run = data?.runs[row.runId];
          if (run === undefined) throw new Error('a row is of a run the host holds');
          const kinds = row.attention.map(attention => attention.kind);
          // Each reason at most once, in the order of `ATTENTION_KINDS`.
          expect(kinds).toEqual(ATTENTION_KINDS.filter(kind => kinds.includes(kind)));
          expect(row.attention).toEqual(
            reasonsOf(run, data?.searches[run.id]?.troubles.length ?? 0),
          );
          expect(row.deadlineMs).toBe(secondsMs(run.deadline));
        }
      }),
    );
  });

  it('never says a run wants a look for how its host is read: freshness changes no reason', () => {
    fc.assert(
      fc.property(fleet, fc.constantFrom(...FRESHNESS), (hosts, freshness) => {
        const reasons = (each: readonly FleetHostInput[]) =>
          shown(each, NO_FLEET_FILTER).map(row => [row.key, row.attention]);
        const refreshed = hosts.map(host => ({...host, freshness, staleForMs: null}));
        expect(reasons(refreshed)).toEqual(reasons(hosts));
      }),
    );
  });

  it('is total on a host that has said nothing yet: no rows, and the host still listed', () => {
    const view = fleetView(
      [
        {
          key: 'a',
          label: 'A',
          data: emptyState,
          runIndex: DORMANT_GENERIC_RUN_INDEX,
          freshness: 'empty',
          status: {kind: 'waiting', cause: null, staleForMs: null},
        },
      ],
      NO_FLEET_FILTER,
    );
    expect(view.live).toEqual([]);
    expect(view.history).toEqual([]);
    expect(view.hosts).toEqual([
      {
        key: 'a',
        label: 'A',
        discovery: {
          managed: {
            freshness: 'empty',
            status: {kind: 'waiting', cause: null, staleForMs: null},
          },
          generic: {kind: 'opening'},
        },
        runs: 0,
        runsLeftOut: 0,
      },
    ]);
  });
});

/** A host reference the overview never reads; present only so a row typechecks. */
const OVERVIEW_HOST: FleetHostRef = {
  key: 'host-0',
  label: 'Host 0',
  freshness: 'live',
  status: {kind: 'live', cause: null, staleForMs: null},
};

/** What one row of the overview fold is made from; the fields `fleetOverview` actually reads. */
interface OverviewRowSpec {
  readonly live: boolean;
  /** A warn-level attention fact (a stall): a call for a look. */
  readonly warn: boolean;
  /** A final-report attention fact: present, but not a call for a look. */
  readonly final: boolean;
  readonly reportUnavailable: boolean;
  readonly agents: number | null;
  readonly cost: number | null;
}

const overviewRow = (spec: OverviewRowSpec, index: number): FleetRow => ({
  source: 'managed',
  setup: false,
  status: spec.live ? RunStatus.RUNNING : RunStatus.ENDED,
  key: `host-0/run-${index}`,
  host: OVERVIEW_HOST,
  runId: `run-${index}`,
  name: `run-${index}`,
  card: null,
  repository: '',
  live: spec.live,
  section: spec.live ? 'live' : 'history',
  startedMs: null,
  endedMs: null,
  objectives: [],
  costUsd: spec.cost,
  budgetUsd: null,
  agentsRunning: spec.agents,
  lastReportMs: null,
  reportUnavailable: spec.reportUnavailable,
  deadlineMs: null,
  attention: [
    ...(spec.warn ? [{kind: 'stalled', sinceMs: 0} as const] : []),
    ...(spec.final ? [{kind: 'final'} as const] : []),
  ],
});

const overviewSpec: fc.Arbitrary<OverviewRowSpec> = fc.record({
  live: fc.boolean(),
  warn: fc.boolean(),
  final: fc.boolean(),
  reportUnavailable: fc.boolean(),
  agents: fc.option(fc.integer({min: 0, max: 20}), {nil: null}),
  cost: fc.option(
    fc.integer({min: 0, max: 100_000}).map(cents => cents / 100),
    {nil: null},
  ),
});

/** Sum over a subset, kept null when the subset reported nothing, as the overview does. */
const sumOrNull = (values: readonly (number | null)[]): number | null => {
  const present = values.filter((value): value is number => value !== null);
  return present.length === 0 ? null : present.reduce((total, value) => total + value, 0);
};

describe('fleetOverview', () => {
  it('reads the empty fleet as zeros with no summed figure', () => {
    expect(fleetOverview([])).toEqual({
      shown: 0,
      live: 0,
      ended: 0,
      attention: 0,
      agentsWorking: null,
      liveCostUsd: null,
    });
  });

  it('counts, and sums only over live runs, matching an independent reading of the rows', () => {
    fc.assert(
      fc.property(fc.array(overviewSpec, {maxLength: 30}), specs => {
        const rows = specs.map(overviewRow);
        const liveSpecs = specs.filter(spec => spec.live);
        expect(fleetOverview(rows)).toEqual({
          shown: specs.length,
          live: liveSpecs.length,
          ended: specs.filter(spec => !spec.live).length,
          attention: liveSpecs.filter(spec => spec.warn || spec.reportUnavailable).length,
          agentsWorking: sumOrNull(liveSpecs.map(spec => spec.agents)),
          liveCostUsd: sumOrNull(liveSpecs.map(spec => spec.cost)),
        });
      }),
    );
  });

  it('does not count a final report, or any ended run, as wanting a look', () => {
    const finalOnly = overviewRow(
      {live: true, warn: false, final: true, reportUnavailable: false, agents: null, cost: null},
      0,
    );
    const endedButTroubled = overviewRow(
      {live: false, warn: true, final: false, reportUnavailable: true, agents: null, cost: null},
      1,
    );
    expect(fleetOverview([finalOnly, endedButTroubled]).attention).toBe(0);
  });

  it('never sums an ended run’s cost or agents into the live figures', () => {
    const endedWithCost = overviewRow(
      {live: false, warn: false, final: false, reportUnavailable: false, agents: 3, cost: 9},
      0,
    );
    const overview = fleetOverview([endedWithCost]);
    expect(overview.agentsWorking).toBeNull();
    expect(overview.liveCostUsd).toBeNull();
  });
});
