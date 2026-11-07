import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  Direction,
  MessageSchema,
  ReportKind,
  RunSchema,
  RunStatus,
  SearchStateSchema,
  UnreadableReason,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {emptyState, type OrchestratorState} from '../state.js';
import {agentsStrip} from './agents-strip.js';
import {objectiveReports} from './objectives.js';
import {runReport} from './run-report.js';
import {latestReport} from './summary.js';
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
        const reports = objectiveReports(create(SearchStateSchema, {series: served}));
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
          expect(report.best).toBe(report.bestPath.at(-1));
        });
      }),
      {seed: 11},
    );
  });

  it('show the head value and the percent of the bound exactly as served, whatever they are', () => {
    fc.assert(
      fc.property(
        fc.double({min: 1e-3, max: 1e9, noNaN: true}),
        fc.double({min: 1e-3, max: 1e9, noNaN: true}),
        fc.double({min: 1e-3, max: 1e4, noNaN: true}),
        (bound, headValue, percentOfBound) => {
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
                  headValue,
                  percentOfBound,
                },
              ],
            }),
          );
          expect(report?.headValue).toBe(headValue);
          expect(report?.percentOfBound).toBe(percentOfBound);
          expect(report?.unit).toBe('tok/s');
          expect(report?.bound?.current.value).toBe(bound);
        },
      ),
      {seed: 12},
    );
  });

  it('say nothing of a percent the host did not send, and read the head for a value of an objective with no bound', () => {
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
    const [report] = objectiveReports(search);
    expect(report?.headValue).toBe(7);
    expect(report?.percentOfBound).toBeUndefined();
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
    );
    expect([measured?.name, bare?.name, bare?.points.length, bare?.better]).toEqual([
      'b',
      'a',
      0,
      'lower',
    ]);
  });

  it('are none for a search with nothing measured or declared', () => {
    expect(objectiveReports(create(SearchStateSchema, {}))).toEqual([]);
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
      value: {headline: `h-${id}`, kind: ReportKind.PROGRESS, sequence, ...extra},
    },
  });

const withMessages = (...messages: ReturnType<typeof report>[]): OrchestratorState => ({
  ...emptyState,
  messages: Object.fromEntries(messages.map(m => [m.id, m])),
});

describe('the lead summary', () => {
  it('is the report with the highest number whatever order they arrive in, and never another run’s', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.integer({min: 1, max: 50}), {minLength: 1, maxLength: 10}),
        fc.nat(1000),
        (sequences, shuffle) => {
          const mine = sequences.map((n, i) =>
            report(`m${i}`, 'r', n, 100 + ((i * 7 + shuffle) % 13)),
          );
          const other = report('x', 'other', 999, 9999);
          const state = withMessages(other, ...mine);
          expect(latestReport(state, 'r')?.sequence).toBe(Math.max(...sequences));
          expect(latestReport(state, 'other')?.id).toBe('x');
        },
      ),
      {seed: 14},
    );
  });

  it('keeps only the parts the lead filled in, in the lead’s order, and says when it is final', () => {
    const state = withMessages(
      report('a', 'r', 1, 1, {
        changed: 'c',
        openForUser: 'o',
        kind: ReportKind.FINAL,
      }),
    );
    const summary = latestReport(state, 'r');
    expect(summary?.fields).toEqual([
      {field: 'changed', text: 'c'},
      {field: 'openForUser', text: 'o'},
    ]);
    expect(summary?.final).toBe(true);
    expect(summary?.atMs).toBe(1000);
  });

  it('is absent before the lead has posted a report', () => {
    expect(latestReport(emptyState, 'r')).toBeUndefined();
    const text = create(MessageSchema, {id: 't', runId: 'r', body: {case: 'text', value: 'hi'}});
    expect(latestReport({...emptyState, messages: {t: text}}, 'r')).toBeUndefined();
  });
});

const agent = (id: string, runId: string, role: AgentRole, status: AgentStatus) =>
  create(AgentSchema, {id, runId, role, status});

describe('the agents strip', () => {
  const roles = fc.constantFrom(
    AgentRole.LEAD,
    AgentRole.PLANNER,
    AgentRole.WORKER,
    AgentRole.JUDGE,
    AgentRole.COMBINER,
    AgentRole.PROFILER,
  );
  const statuses = fc.constantFrom(
    AgentStatus.LAUNCHING,
    AgentStatus.ACTIVE,
    AgentStatus.WAITING,
    AgentStatus.IDLE,
    AgentStatus.ENDED,
    AgentStatus.FAILED,
    AgentStatus.LOST,
  );

  it('counts the run’s agents once, by type, and the running ones among them', () => {
    fc.assert(
      fc.property(
        fc.array(fc.tuple(roles, statuses, fc.constantFrom('r', 'other')), {maxLength: 30}),
        rows => {
          const state: OrchestratorState = {
            ...emptyState,
            agents: Object.fromEntries(
              rows.map(([role, status, runId], i) => [
                `a${i}`,
                agent(`a${i}`, runId, role, status),
              ]),
            ),
          };
          const mine = rows.filter(([, , runId]) => runId === 'r');
          const strip = agentsStrip(state, 'r');
          expect(strip.total).toBe(mine.length);
          expect(strip.types.reduce((n, t) => n + t.total, 0)).toBe(mine.length);
          expect(strip.running).toBe(strip.types.reduce((n, t) => n + t.running, 0));
          expect(strip.running).toBe(
            mine.filter(([, s]) => s === AgentStatus.ACTIVE || s === AgentStatus.LAUNCHING).length,
          );
          for (const type of strip.types) {
            expect(type.total).toBe(mine.filter(([role]) => role === type.role).length);
            expect(type.running).toBeLessThanOrEqual(type.total);
          }
          expect(new Set(strip.types.map(t => t.role)).size).toBe(strip.types.length);
        },
      ),
      {seed: 15},
    );
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
    expect(stale?.search).toEqual({state: 'unreadable', sinceMs: 5000});
    expect(stale?.truncated).toBe(true);
  });
});
