import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  Direction,
  MessageSchema,
  ObjectiveStandingSchema,
  PlanStatus,
  ReportKind,
  ReportScope,
  RoleAgentsSchema,
  RunSchema,
  RunStatus,
  RunSummarySchema,
  SearchStateSchema,
  UnreadableReason,
  UnreadableReasonSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {emptyState, type OrchestratorState} from '../state.js';
import {agentsStrip} from './agents-strip.js';
import {objectiveReports} from './objectives.js';
import {runReport} from './run-report.js';
import {summaryReport} from './summary.js';
import {tradeoffReport} from './tradeoff.js';

const direction = fc.constantFrom(Direction.MAXIMIZE, Direction.MINIMIZE);

const point = fc.record({
  jobId: fc.string({minLength: 1, maxLength: 8}),
  commit: fc.stringMatching(/^[0-9a-f]{4,8}$/),
  at: fc.integer({min: 1, max: 1_000_000}).map(s => timestampFromMs(s * 1000)),
  value: fc.double({min: -1e6, max: 1e6, noNaN: true}),
  bestSoFar: fc.boolean(),
});

const series = fc.uniqueArray(
  fc.record({
    objective: fc.constantFrom('tok_s', 'p99_ms', 'mem_gb', 'cost'),
    direction,
    points: fc.array(point, {minLength: 1, maxLength: 12}),
  }),
  {selector: row => row.objective, maxLength: 4},
);

describe('the objectives of a report', () => {
  it('keep every point, mark and objective as served, in the order served', () => {
    fc.assert(
      fc.property(series, served => {
        const reports = objectiveReports(create(SearchStateSchema, {series: served}), []);
        expect(reports.map(r => r.name)).toEqual(served.map(s => s.objective));
        reports.forEach((report, index) => {
          const from = served[index];
          expect(report.better).toBe(from?.direction === Direction.MINIMIZE ? 'lower' : 'higher');
          expect(report.points.map(p => [p.id, p.value, p.bestSoFar])).toEqual(
            from?.points.map(p => [p.jobId, p.value, p.bestSoFar]),
          );
          // The best-so-far line is the marked points and the best is the last of them: nothing is
          // compared here.
          expect(report.bestPath).toEqual(report.points.filter(p => p.bestSoFar));
          // The series draws the line only: the served best is the standing's, absent with none.
          expect(report.bestValue).toBeUndefined();
        });
      }),
      {seed: 11},
    );
  });

  it('show the head value and the percents of the bound exactly as the run summary and the bounds served them, whatever they are', () => {
    fc.assert(
      fc.property(
        fc.double({min: 1e-3, max: 1e9, noNaN: true}),
        fc.double({min: 1e-3, max: 1e9, noNaN: true}),
        fc.double({min: 1e-3, max: 1e4, noNaN: true}),
        fc.double({min: 1e-3, max: 1e4, noNaN: true}),
        (bound, headValue, percentOfBound, bestPercentOfBound) => {
          const [report] = objectiveReports(
            create(SearchStateSchema, {
              series: [
                {
                  objective: 'tok_s',
                  direction: Direction.MAXIMIZE,
                  points: [{jobId: 'j', commit: 'c', at: timestampFromMs(1000), value: 1}],
                },
              ],
              bounds: [
                {
                  objective: 'tok_s',
                  direction: Direction.MAXIMIZE,
                  versions: [{seq: 1n, value: bound, unit: 'tok/s', note: 'n', objective: 'tok_s'}],
                  // The search state's own head figures are not read: the summary's are.
                  headValue: headValue + 1,
                  percentOfBound: percentOfBound + 1,
                  bestPercentOfBound,
                },
              ],
            }),
            [
              create(ObjectiveStandingSchema, {
                name: 'tok_s',
                headValue,
                headPercentOfBound: percentOfBound,
                bestPercentOfBound,
                unit: 'tok/s',
              }),
            ],
          );
          expect(report?.headValue).toBe(headValue);
          expect(report?.percentOfBound).toBe(percentOfBound);
          expect(report?.bestPercentOfBound).toBe(bestPercentOfBound);
          expect(report?.unit).toBe('tok/s');
          expect(report?.bound?.current.value).toBe(bound);
        },
      ),
      {seed: 12},
    );
  });

  it('show the best, unit and percent the standing served, not the series’ newest best or the bound’s unit, whatever they disagree by', () => {
    fc.assert(
      fc.property(
        fc.double({min: 1, max: 1e6, noNaN: true}),
        fc.double({min: 1, max: 1e6, noNaN: true}),
        fc.double({min: 1e-3, max: 1e4, noNaN: true}),
        (seriesBest, servedBest, percent) => {
          const [report] = objectiveReports(
            create(SearchStateSchema, {
              series: [
                {
                  objective: 'tok_s',
                  direction: Direction.MAXIMIZE,
                  points: [
                    {
                      jobId: 'j',
                      commit: 'c',
                      at: timestampFromMs(1000),
                      value: seriesBest,
                      bestSoFar: true,
                    },
                  ],
                },
              ],
              bounds: [
                {
                  objective: 'tok_s',
                  direction: Direction.MAXIMIZE,
                  versions: [
                    {seq: 1n, value: 9, unit: 'from-bound', note: 'n', objective: 'tok_s'},
                  ],
                  bestPercentOfBound: percent + 1,
                },
              ],
            }),
            [
              create(ObjectiveStandingSchema, {
                name: 'tok_s',
                bestValue: servedBest,
                bestPercentOfBound: percent,
                unit: 'tok/s',
              }),
            ],
          );
          expect(report?.bestValue).toBe(servedBest);
          expect(report?.bestPercentOfBound).toBe(percent);
          expect(report?.unit).toBe('tok/s');
        },
      ),
      {seed: 13},
    );
  });

  it('have no head value or percent for an objective the summary served no head for, and compute none from the search state', () => {
    const search = create(SearchStateSchema, {
      series: [
        {
          objective: 'score',
          direction: Direction.MAXIMIZE,
          points: [{jobId: 'j', commit: 'c', at: timestampFromMs(1000), value: 3}],
        },
      ],
      head: {
        state: {
          case: 'current',
          value: {
            head: 'h',
            result: {
              outcome: {case: 'measure', value: {metric: {name: 'score'}, value: 7}},
            },
          },
        },
      },
    });
    const [report] = objectiveReports(search, []);
    expect(report?.headValue).toBeUndefined();
    expect(report?.percentOfBound).toBeUndefined();
    expect(report?.bestPercentOfBound).toBeUndefined();
    expect(report?.bound).toBeUndefined();
    expect(report?.unit).toBeNull();
  });

  it('list an objective that has a bound and no measurement after the measured ones', () => {
    const [measured, bare] = objectiveReports(
      create(SearchStateSchema, {
        series: [
          {
            objective: 'b',
            direction: Direction.MAXIMIZE,
            points: [{jobId: 'j', commit: 'c', at: timestampFromMs(1000), value: 1}],
          },
        ],
        bounds: [
          {
            objective: 'a',
            direction: Direction.MINIMIZE,
            versions: [{seq: 1n, value: 5, unit: 'ms', note: 'n', objective: 'a'}],
          },
        ],
      }),
      [
        create(ObjectiveStandingSchema, {name: 'b'}),
        create(ObjectiveStandingSchema, {name: 'a', headValue: 4}),
      ],
    );
    expect(bare?.headValue).toBe(4);
    expect([measured?.name, bare?.name, bare?.points.length, bare?.better]).toEqual([
      'b',
      'a',
      0,
      'lower',
    ]);
  });

  it('keeps a summary-only declared objective and merges series and bounds into the summary order without inventing its direction', () => {
    const reports = objectiveReports(
      create(SearchStateSchema, {
        series: [
          {
            objective: 'measured',
            direction: Direction.MAXIMIZE,
            points: [{jobId: 'j', commit: 'c', at: timestampFromMs(1000), value: 3}],
          },
        ],
        bounds: [
          {
            objective: 'bounded',
            direction: Direction.MINIMIZE,
            versions: [{seq: 1n, value: 5, unit: 'ms', note: 'n', objective: 'bounded'}],
          },
        ],
      }),
      [
        create(ObjectiveStandingSchema, {name: 'declared-only', headValue: 7}),
        create(ObjectiveStandingSchema, {name: 'measured', bestValue: 3}),
        create(ObjectiveStandingSchema, {name: 'bounded'}),
      ],
    );

    expect(reports.map(report => report.name)).toEqual(['declared-only', 'measured', 'bounded']);
    expect(reports.map(report => report.better)).toEqual([null, 'higher', 'lower']);
    expect(reports[0]).toMatchObject({headValue: 7, points: [], bound: undefined});
    expect(reports[1]?.points).toHaveLength(1);
    expect(reports[2]?.bound?.current.value).toBe(5);
  });

  it('lists every summary, series and bound objective once, with the summary order first', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.stringMatching(/^[a-z]{1,8}$/), {minLength: 1, maxLength: 8}),
        names => {
          const serial = names.filter((_, at) => at % 2 === 0);
          const bounded = names.filter((_, at) => at % 3 === 0);
          const extra = 'outside-summary';
          const reports = objectiveReports(
            create(SearchStateSchema, {
              series: [...serial, extra].map((objective, at) => ({
                objective,
                direction: at % 2 === 0 ? Direction.MAXIMIZE : Direction.MINIMIZE,
                points: [
                  {
                    jobId: `job-${at}`,
                    commit: `c${at}`,
                    at: timestampFromMs(at + 1),
                    value: at,
                  },
                ],
              })),
              bounds: bounded.map((objective, at) => ({
                objective,
                direction: Direction.MINIMIZE,
                versions: [{seq: 1n, value: at + 1, unit: 'u', note: '', objective}],
              })),
            }),
            names.map(name => create(ObjectiveStandingSchema, {name})),
          );
          expect(reports.map(report => report.name)).toEqual([...names, extra]);
          expect(new Set(reports.map(report => report.name)).size).toBe(reports.length);
          for (const report of reports) {
            if (serial.includes(report.name) || report.name === extra) {
              expect(report.better).not.toBeNull();
            } else if (bounded.includes(report.name)) {
              expect(report.better).toBe('lower');
            } else {
              expect(report.better).toBeNull();
            }
          }
        },
      ),
      {seed: 14},
    );
  });

  it('are none for a search with nothing measured or declared', () => {
    expect(objectiveReports(create(SearchStateSchema, {}), [])).toEqual([]);
  });
});

describe('the trade-off of a report', () => {
  const tradeoff = fc.integer({min: 2, max: 4}).chain(width =>
    fc
      .array(
        fc.record({
          jobId: fc.string({minLength: 1, maxLength: 6}),
          values: fc.array(fc.double({min: -100, max: 100, noNaN: true}), {
            minLength: width,
            maxLength: width,
          }),
          onFrontier: fc.boolean(),
        }),
        {maxLength: 20},
      )
      .map(points => ({width, points})),
  );

  it('keeps the host’s frontier marks and values, and counts the marks', () => {
    fc.assert(
      fc.property(tradeoff, ({width, points}) => {
        const search = create(SearchStateSchema, {
          tradeoff: {
            objectives: Array.from({length: width}, (_, i) => ({
              name: `o${i}`,
              direction: i % 2 === 0 ? Direction.MAXIMIZE : Direction.MINIMIZE,
            })),
            points: points.map(p => ({...p, commit: 'c', at: timestampFromMs(1000)})),
          },
        });
        const report = tradeoffReport(search);
        expect(report?.objectives.map(o => o.better)).toEqual(
          Array.from({length: width}, (_, i) => (i % 2 === 0 ? 'higher' : 'lower')),
        );
        expect(report?.points.map(p => [p.id, p.values, p.onFrontier])).toEqual(
          points.map(p => [p.jobId, p.values, p.onFrontier]),
        );
        expect(report?.frontierCount).toBe(points.filter(p => p.onFrontier).length);
      }),
      {seed: 13},
    );
  });

  it('is absent when the host sent none or fewer than two objectives', () => {
    expect(tradeoffReport(create(SearchStateSchema, {}))).toBeUndefined();
    expect(
      tradeoffReport(
        create(SearchStateSchema, {
          tradeoff: {objectives: [{name: 'a', direction: Direction.MAXIMIZE}]},
        }),
      ),
    ).toBeUndefined();
  });
});

const report = (id: string, runId: string, sequence: number, atS: number, extra = {}) =>
  create(MessageSchema, {
    id,
    runId,
    at: timestampFromMs(atS * 1000),
    body: {
      case: 'report',
      value: {
        headline: `h-${id}`,
        kind: ReportKind.PROGRESS,
        scope: ReportScope.RUN,
        sequence,
        ...extra,
      },
    },
  });

const withSummary = (runId: string, message?: ReturnType<typeof report>): OrchestratorState => ({
  ...emptyState,
  summaries: {
    [runId]: create(RunSummarySchema, {runId, ...(message === undefined ? {} : {report: message})}),
  },
});

describe('the lead summary', () => {
  // The host picks the report that sums a run up. Which messages the app holds, in what order, of
  // whatever scope, changes nothing: the summary is the one report the host served.
  it('is the report the host served, whatever messages are held of the run, a window report among them', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.integer({min: 1, max: 50}), {minLength: 1, maxLength: 10}),
        fc.constantFrom(ReportScope.RUN, ReportScope.WINDOW, ReportScope.UNSPECIFIED),
        (sequences, scope) => {
          const held = sequences.map((n, i) => report(`m${i}`, 'r', n, 100 + i, {scope}));
          const served = report('served', 'r', 1, 5);
          const state: OrchestratorState = {
            ...withSummary('r', served),
            messages: Object.fromEntries(held.map(m => [m.id, m])),
          };
          expect(summaryReport(state, 'r')?.id).toBe('served');
          // Another run's summary is its own.
          expect(summaryReport(state, 'other')).toBeUndefined();
        },
      ),
      {seed: 14},
    );
  });

  it('keeps only the parts the lead filled in, in the lead’s order, and says when it is final', () => {
    const state = withSummary(
      'r',
      report('a', 'r', 1, 1, {
        changed: 'c',
        decisions: 'o',
        kind: ReportKind.FINAL,
      }),
    );
    const summary = summaryReport(state, 'r');
    expect(summary?.fields).toEqual([
      {field: 'changed', text: 'c'},
      {field: 'decisions', text: 'o'},
    ]);
    expect(summary?.final).toBe(true);
    expect(summary?.atMs).toBe(1000);
  });

  it('is absent before the host has served a report, and a message that is not a report is none', () => {
    expect(summaryReport(emptyState, 'r')).toBeUndefined();
    expect(summaryReport(withSummary('r'), 'r')).toBeUndefined();
    const text = create(MessageSchema, {id: 't', runId: 'r', body: {case: 'text', value: 'hi'}});
    expect(summaryReport(withSummary('r', text), 'r')).toBeUndefined();
  });
});

describe('the agents strip', () => {
  const roles = fc.constantFrom(
    AgentRole.LEAD,
    AgentRole.PLANNER,
    AgentRole.WORKER,
    AgentRole.JUDGE,
    AgentRole.COMBINER,
    AgentRole.PROFILER,
  );

  it('is the agents of the run as the host counted them, by type, in the order served', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(
          fc.record({
            role: roles,
            total: fc.integer({min: 1, max: 9}),
            working: fc.integer({min: 0, max: 9}),
          }),
          {selector: row => row.role, maxLength: 6},
        ),
        rows => {
          const served = rows.map(row => ({...row, working: Math.min(row.working, row.total)}));
          const state: OrchestratorState = {
            ...emptyState,
            summaries: {
              r: create(RunSummarySchema, {
                runId: 'r',
                agentsTotal: served.reduce((n, row) => n + row.total, 0),
                agentsWorking: served.reduce((n, row) => n + row.working, 0),
                roles: served.map(row => create(RoleAgentsSchema, row)),
              }),
            },
          };
          const strip = agentsStrip(state, 'r');
          expect(strip.types).toEqual(
            served.map(({role, total, working}) => ({role, total, running: working})),
          );
          expect(strip.total).toBe(state.summaries['r']?.agentsTotal);
          expect(strip.running).toBe(state.summaries['r']?.agentsWorking);
        },
      ),
      {seed: 15},
    );
  });

  it('is empty before the host has served the run’s summary', () => {
    expect(agentsStrip(emptyState, 'r')).toEqual({types: [], total: 0, running: 0});
  });
});

describe('the run report', () => {
  const run = (fields = {}) =>
    create(RunSchema, {
      id: 'r',
      task: 'faster-decode',
      status: RunStatus.RUNNING,
      createdAt: timestampFromMs(10_000),
      ...fields,
    });
  const stateOf = (fields = {}): OrchestratorState => ({...emptyState, runs: {r: run(fields)}});

  it('is absent for a run the host does not have', () => {
    expect(runReport(emptyState, 'r', 1)).toBeUndefined();
  });

  it('counts a live run’s elapsed time to now and an ended run’s to its end, never negative', () => {
    fc.assert(
      fc.property(fc.integer({min: 0, max: 1e6}), fc.integer({min: 0, max: 1e6}), (now, end) => {
        const live = runReport(stateOf(), 'r', now);
        expect(live?.header.elapsedMs).toBe(Math.max(0, now - 10_000));
        const ended = runReport(
          stateOf({status: RunStatus.ENDED, endedAt: timestampFromMs(10_000 + end)}),
          'r',
          now,
        );
        expect(ended?.header.elapsedMs).toBe(end);
        expect(ended?.span).toEqual({fromMs: 10_000, toMs: 10_000 + end});
      }),
      {seed: 16},
    );
  });

  it('says whether the search facts are being read', () => {
    const none = runReport(stateOf(), 'r', 20_000);
    expect(none?.search).toEqual({state: 'none'});
    const live = runReport(
      {
        ...stateOf(),
        searches: {
          r: create(SearchStateSchema, {
            runId: 'r',
            health: {state: {case: 'liveSince', value: timestampFromMs(1000)}},
          }),
        },
      },
      'r',
      20_000,
    );
    expect(live?.search).toEqual({state: 'live'});
    const stale = runReport(
      {
        ...stateOf(),
        searches: {
          r: create(SearchStateSchema, {
            runId: 'r',
            truncated: true,
            health: {
              state: {
                case: 'unreadable',
                value: {reason: UnreadableReason.STREAM_LOST, since: timestampFromMs(5000)},
              },
            },
          }),
        },
      },
      'r',
      20_000,
    );
    expect(stale?.search).toEqual({
      state: 'unreadable',
      reason: UnreadableReason.STREAM_LOST,
      detail: '',
      sinceMs: 5000,
    });
    expect(stale?.truncated).toBe(true);
  });

  it('carries why the search cannot be read, the host’s words and since when, or that it said no time', () => {
    const reasons = UnreadableReasonSchema.values.map(value => value.number as UnreadableReason);
    fc.assert(
      fc.property(
        fc.constantFrom(...reasons),
        fc.string({maxLength: 64}),
        fc.option(fc.integer({min: 0, max: 4e12}), {nil: undefined}),
        (reason, detail, sinceMs) => {
          const search = create(SearchStateSchema, {
            runId: 'r',
            health: {
              state: {
                case: 'unreadable',
                value: {
                  reason,
                  detail,
                  ...(sinceMs === undefined ? {} : {since: timestampFromMs(sinceMs)}),
                },
              },
            },
          });
          const report = runReport({...stateOf(), searches: {r: search}}, 'r', 20_000);
          expect(report?.search).toEqual({
            state: 'unreadable',
            reason,
            detail,
            sinceMs: sinceMs ?? null,
          });
        },
      ),
    );
  });

  it('has no plans view before a search state is served', () => {
    expect(runReport(stateOf(), 'r', 20_000)?.plans).toBeUndefined();
  });

  it('keeps the summary objective before the disjoint search stream is served', () => {
    const state: OrchestratorState = {
      ...stateOf(),
      summaries: {
        r: create(RunSummarySchema, {
          runId: 'r',
          objectives: [{name: 'tail latency'}],
        }),
      },
    };

    expect(runReport(state, 'r', 20_000)?.objectives).toMatchObject([
      {name: 'tail latency', better: null, points: [], bestValue: undefined},
    ]);
  });

  it('counts the run’s plans by status, keeping Chosen and Won apart, with the host’s words', () => {
    const search = create(SearchStateSchema, {
      runId: 'r',
      plans: [
        {number: 1, status: PlanStatus.OPEN},
        {number: 2, status: PlanStatus.CHOSEN},
        {number: 3, status: PlanStatus.WON},
        {number: 4, status: PlanStatus.PARKED},
        {number: 5, status: PlanStatus.REJECTED},
        {number: 6, status: PlanStatus.OPEN},
      ],
      unattributedJobs: 3,
    });
    const report = runReport({...stateOf(), searches: {r: search}}, 'r', 20_000);
    expect(report?.plans?.tallies.map(tally => [tally.label, tally.count])).toEqual([
      ['Open', 2],
      ['Chosen', 1],
      ['Won', 1],
      ['Parked', 1],
      ['Rejected', 1],
    ]);
    expect(report?.plans?.total).toBe(6);
    expect(report?.plans?.withoutRun).toBe(3);
  });

  it('counts every status in one fixed order and carries the jobs no run claimed, over any plans', () => {
    const status = fc.constantFrom(
      PlanStatus.OPEN,
      PlanStatus.CHOSEN,
      PlanStatus.WON,
      PlanStatus.PARKED,
      PlanStatus.REJECTED,
      PlanStatus.UNSPECIFIED,
    );
    fc.assert(
      fc.property(
        fc.array(status, {maxLength: 30}),
        fc.integer({min: 0, max: 99}),
        (statuses, unattributedJobs) => {
          const search = create(SearchStateSchema, {
            runId: 'r',
            plans: statuses.map((each, at) => ({number: at + 1, status: each})),
            unattributedJobs,
          });
          const plans = runReport({...stateOf(), searches: {r: search}}, 'r', 20_000)?.plans;
          // The figures are the host's, carried as they are.
          expect(plans?.total).toBe(statuses.length);
          expect(plans?.withoutRun).toBe(unattributedJobs);
          // Each tally counts the plans of its status, and the counts sum to the total.
          for (const tally of plans?.tallies ?? []) {
            expect(tally.count).toBe(statuses.filter(each => each === tally.status).length);
          }
          expect((plans?.tallies ?? []).reduce((sum, tally) => sum + tally.count, 0)).toBe(
            statuses.length,
          );
          // Only the statuses present appear, in the one fixed order, never twice.
          const order = [
            PlanStatus.OPEN,
            PlanStatus.CHOSEN,
            PlanStatus.WON,
            PlanStatus.PARKED,
            PlanStatus.REJECTED,
            PlanStatus.UNSPECIFIED,
          ];
          expect(plans?.tallies.map(tally => tally.status)).toEqual(
            order.filter(each => statuses.includes(each)),
          );
        },
      ),
      {seed: 17},
    );
  });
});
