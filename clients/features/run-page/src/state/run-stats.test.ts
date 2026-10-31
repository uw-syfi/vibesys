/**
 * The stats row over generated runs: agent time sits within running time, counts add up, and every
 * figure of the row is the run's as of the position of the time bar, not the whole run's.
 */
import {describe, expect, test} from 'bun:test';
import type {Span} from '@vibesys/contracts';
import {EMPTY_LEDGER, type TokenLedger, type TokenTally} from '@vibesys/run-state';
import type {RunSession} from '@vibesys/run-state/skeleton';
import fc from 'fast-check';
import type {RunModel} from './run-model.js';
import {runStats, statTiles} from './run-stats.js';
import {runningTimeOf, runningWords} from './running-time.js';
import {
  expectedAt,
  scrubOf,
  shapedRuns,
  startOf,
  viewedAt as viewedLikeTheApp,
  wholeOf,
} from './shaped-runs.test-helper.js';
import {endOf} from './time-position.js';
import {idea, modelOf} from './timeline-fixtures.js';

const turn = fc.record({
  from: fc.integer({min: 0, max: 900}),
  length: fc.integer({min: 1, max: 300}),
  open: fc.boolean(),
});

const sessions: fc.Arbitrary<readonly RunSession[]> = fc
  .array(
    fc.record({work: fc.integer({min: 1, max: 400}), stopped: fc.integer({min: 0, max: 5000})}),
    {
      minLength: 1,
      maxLength: 4,
    },
  )
  .map(parts => {
    let at = 0;
    return parts.map(part => {
      const from = at + part.stopped;
      at = from + part.work;
      return {from, to: at};
    });
  });

function modelWith(
  turns: readonly {from: number; length: number; open: boolean}[],
  runSessions: readonly RunSession[],
): RunModel {
  const spans = new Map<string, Span>(
    turns.map((each, at) => [
      `t${at}`,
      {
        id: `t${at}`,
        label: {kind: 'agent_turn'},
        status: each.open ? 'running' : 'done',
        started_at: each.from,
        ended_at: each.open ? undefined : each.from + each.length,
      } as unknown as Span,
    ]),
  );
  return {
    ...modelOf([idea('a', 'passed', 0, 10), idea('b', 'running', 5, 0)]),
    trace: {spans, sequence: 0},
    sessions: runSessions,
    latest: runSessions.at(-1)?.to ?? 0,
  } as unknown as RunModel;
}

describe('the stats row', () => {
  test('agent time is within the running time the chart’s axis counts, however the turns overlap', () => {
    fc.assert(
      fc.property(fc.array(turn, {maxLength: 12}), sessions, (turns, runSessions) => {
        const model = modelWith(turns, runSessions);
        const stats = runStats(model, endOf(model), 0, EMPTY_LEDGER);
        const running = runningTimeOf(model.sessions ?? [], 0).at(model.latest ?? 0);
        expect(stats.runningSeconds).toBe(running);
        expect(stats.agentSeconds).not.toBeNull();
        expect(stats.agentSeconds as number).toBeGreaterThanOrEqual(0);
        expect(stats.agentSeconds as number).toBeLessThanOrEqual(running as number);
      }),
    );
  });

  test('turns that overlap count once: repeating every turn changes nothing', () => {
    fc.assert(
      fc.property(fc.array(turn, {maxLength: 12}), sessions, (turns, runSessions) => {
        const once = modelWith(turns, runSessions);
        const twice = modelWith([...turns, ...turns], runSessions);
        expect(runStats(twice, endOf(twice), 0, EMPTY_LEDGER).agentSeconds).toBe(
          runStats(once, endOf(once), 0, EMPTY_LEDGER).agentSeconds,
        );
      }),
    );
  });

  test('counts the ideas, those measured and those running, never more measured than tried', () => {
    const model = modelWith([], [{from: 0, to: 10}]);
    const stats = runStats(model, endOf(model), 0, EMPTY_LEDGER);
    expect(stats.ideas).toEqual({tried: 2, measured: 1, running: 1});
    expect(stats.measurements.measured).toBeLessThanOrEqual(stats.ideas.tried);
  });

  test('words the tiles the way the row shows them', () => {
    const model = modelWith([{from: 0, length: 600, open: false}], [{from: 0, to: 1300}]);
    const tiles = statTiles(
      runStats(model, endOf(model), 0, {
        total: {
          input: 8_100_000,
          cacheRead: 6_500_000,
          cacheWrite: null,
          output: 459_000,
          reasoning: null,
        },
        bySpan: {},
      }),
    );
    expect(tiles.map(tile => [tile.label, tile.figure, tile.note])).toEqual([
      ['Ideas tried', '2', '1 measured · 1 running'],
      ['Measurements', '1', '0 failed'],
      ['Agent time', runningWords(600), `of ${runningWords(1300)} running`],
      ['Tokens', '8.1M', '6.5M cached · 459k output'],
    ]);
    expect(statTiles(runStats(model, endOf(model), 0, EMPTY_LEDGER))[3]?.figure).toBe(
      'Not reported',
    );
  });
});

describe('the stats row at a position of the time bar', () => {
  const usageTurn = fc.record({
    from: fc.integer({min: 0, max: 900}),
    length: fc.integer({min: 1, max: 300}),
    open: fc.boolean(),
    input: fc.integer({min: 1, max: 5000}),
    output: fc.integer({min: 1, max: 500}),
  });

  type UsageTurn = {
    readonly from: number;
    readonly length: number;
    readonly open: boolean;
    readonly input: number;
    readonly output: number;
  };

  /** The tally of a turn: only a turn that ended spent anything. */
  const tallyOf = (turn: UsageTurn): TokenTally => ({
    input: turn.input,
    cacheRead: null,
    cacheWrite: null,
    output: turn.output,
    reasoning: null,
  });

  /**
   * The run to its end, its ledger (the turns that ended, by span, and `unnamed` input of turns no
   * span names), and the run as it stood at `until`: the turns begun by then, those not yet ended
   * still running.
   */
  function runAt(
    turns: readonly UsageTurn[],
    runSessions: readonly RunSession[],
    unnamed: number,
    until: number,
  ) {
    const whole = modelWith(turns, runSessions);
    const ledger: TokenLedger = {
      total: {
        input: turns.filter(turn => !turn.open).reduce((sum, turn) => sum + turn.input, unnamed),
        cacheRead: null,
        cacheWrite: null,
        output: turns.some(turn => !turn.open)
          ? turns.filter(turn => !turn.open).reduce((sum, turn) => sum + turn.output, 0)
          : null,
        reasoning: null,
      },
      bySpan: Object.fromEntries(
        turns.flatMap((turn, at) => (turn.open ? [] : [[`t${at}`, tallyOf(turn)] as const])),
      ),
    };
    const spans = new Map<string, Span>(
      [...whole.trace.spans]
        .filter(([, span]) => span.started_at <= until)
        .map(([id, span]) => [
          id,
          span.ended_at != null && span.ended_at <= until
            ? span
            : ({...span, status: 'running', ended_at: undefined} as unknown as Span),
        ]),
    );
    const viewed = {
      ...whole,
      trace: {spans, sequence: 0},
      latest: until,
      candidates: {
        ...whole.candidates,
        candidates: whole.candidates.candidates.filter(each => each.startedAt <= until),
      },
    } as RunModel;
    return {whole, viewed, ledger, position: {whole, scrub: until}};
  }

  test('the tokens are those of the turns that had ended by then; the end holds the whole ledger', () => {
    fc.assert(
      fc.property(
        fc.array(usageTurn, {maxLength: 12}),
        sessions,
        fc.integer({min: 0, max: 100}),
        fc.double({min: 0, max: 1, noNaN: true}),
        (turns, runSessions, unnamed, cursor) => {
          const last = runSessions.at(-1)?.to ?? 0;
          const until = Math.floor(cursor * last);
          const {whole, viewed, ledger, position} = runAt(turns, runSessions, unnamed, until);
          const ended = turns.filter(turn => !turn.open && turn.from + turn.length <= until);
          const stats = runStats(viewed, position, 0, ledger);
          const want = ended.reduce((sum, turn) => sum + turn.input, 0);
          expect(stats.tokens?.input ?? 0).toBe(want);
          expect(stats.tokens?.output ?? 0).toBe(ended.reduce((sum, turn) => sum + turn.output, 0));
          // At the end the row reads the ledger whole, the turns no span names included.
          expect(runStats(whole, endOf(whole), 0, ledger).tokens).toEqual(ledger.total);
        },
      ),
    );
  });

  test('what the run had done only grows with the position: ideas, time, agent time and tokens', () => {
    fc.assert(
      fc.property(
        fc.array(usageTurn, {maxLength: 12}),
        sessions,
        fc.integer({min: 0, max: 100}),
        fc.tuple(
          fc.double({min: 0, max: 1, noNaN: true}),
          fc.double({min: 0, max: 1, noNaN: true}),
        ),
        (turns, runSessions, unnamed, [x, y]) => {
          const last = runSessions.at(-1)?.to ?? 0;
          const [early, late] = [
            Math.floor(Math.min(x, y) * last),
            Math.floor(Math.max(x, y) * last),
          ];
          const at = (until: number) => {
            const {viewed, ledger, position} = runAt(turns, runSessions, unnamed, until);
            return runStats(viewed, position, 0, ledger);
          };
          const [a, b] = [at(early), at(late)];
          expect(a.ideas.tried).toBeLessThanOrEqual(b.ideas.tried);
          expect(a.runningSeconds ?? 0).toBeLessThanOrEqual(b.runningSeconds ?? 0);
          expect(a.agentSeconds ?? 0).toBeLessThanOrEqual(b.agentSeconds ?? 0);
          expect(a.tokens?.input ?? 0).toBeLessThanOrEqual(b.tokens?.input ?? 0);
          // Its time is the running time the chart's axis counts, at the position, not at the end.
          expect(a.runningSeconds).toBe(runningTimeOf(runSessions, 0).at(early));
          // The ideas of `modelWith` began at 0 and at 5.
          expect(a.ideas.tried).toBe([0, 5].filter(began => began <= early).length);
        },
      ),
    );
  });

  test('the ideas tried and measured are those the run had begun and read by the position', () => {
    fc.assert(
      fc.property(shapedRuns, each => {
        const whole = wholeOf(each);
        const scrub = scrubOf(each);
        const viewed = viewedLikeTheApp(each, whole, scrub);
        const stats = runStats(viewed, {whole, scrub}, 0, EMPTY_LEDGER);
        expect(stats.ideas.tried).toBe(each.ideas.filter((_, at) => startOf(at) <= scrub).length);
        expect(stats.ideas.measured).toBe(expectedAt(each, 0, scrub).cells.size);
        const end = runStats(whole, endOf(whole), 0, EMPTY_LEDGER);
        expect(end.ideas.tried).toBe(each.ideas.length);
        expect(end.ideas.measured).toBe(expectedAt(each, 0, null).cells.size);
      }),
    );
  });

  test('a turn still running at the position has spent nothing yet, though the run’s ledger has its usage', () => {
    const turn = {from: 0, length: 100, open: false, input: 700, output: 30};
    const sessions: readonly RunSession[] = [{from: 0, to: 200}];
    const at = (until: number) => {
      const {viewed, ledger, position} = runAt([turn], sessions, 0, until);
      return runStats(viewed, position, 0, ledger).tokens?.input ?? 0;
    };
    expect(at(50)).toBe(0);
    expect(at(99)).toBe(0);
    expect(at(100)).toBe(700);
    expect(at(150)).toBe(700);
  });

  test('measurements count the attempts that measured a value and those that failed to, not both', () => {
    const base = idea('x', 'passed', 0, 10);
    const measured = base.attempts[0]?.measurement ?? null;
    if (measured === null) throw new Error('the fixture idea has a measurement');
    const attempts = [
      base.attempts[0],
      // A gate that read nothing and failed its check.
      {...base.attempts[0], measurement: {...measured, metricValue: null, verdict: 'failed_check'}},
      // An attempt the run recorded as never measured.
      {...base.attempts[0], reason: 'not_measured', measurement: null},
      // An attempt turned down before any gate.
      {...base.attempts[0], reason: 'turned_down', measurement: null},
    ] as unknown as typeof base.attempts;
    const model = {
      ...modelWith([], [{from: 0, to: 10}]),
      candidates: {...modelOf([]).candidates, candidates: [{...base, attempts}]},
    } as RunModel;
    expect(runStats(model, endOf(model), 0, EMPTY_LEDGER).measurements).toEqual({
      measured: 1,
      failed: 2,
    });
  });
});
