/** The stats row over generated runs: agent time sits within running time, and counts add up. */
import {describe, expect, test} from 'bun:test';
import type {Span} from '@vibesys/contracts';
import type {RunSession} from '@vibesys/run-state/skeleton';
import fc from 'fast-check';
import type {RunModel} from './run-model.js';
import {runStats, statTiles} from './run-stats.js';
import {runningSeconds} from './running-time.js';
import {workedWords} from './time-words.js';
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
        const stats = runStats(model, model, 0, null);
        const running = runningSeconds(model, 0);
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
        expect(runStats(twice, twice, 0, null).agentSeconds).toBe(
          runStats(once, once, 0, null).agentSeconds,
        );
      }),
    );
  });

  test('counts the ideas, those measured and those running, never more measured than tried', () => {
    const model = modelWith([], [{from: 0, to: 10}]);
    const stats = runStats(model, model, 0, null);
    expect(stats.ideas).toEqual({tried: 2, measured: 1, running: 1});
    expect(stats.measurements.measured).toBeLessThanOrEqual(stats.ideas.tried);
  });

  test('words the tiles the way the row shows them', () => {
    const model = modelWith([{from: 0, length: 600, open: false}], [{from: 0, to: 1300}]);
    const tiles = statTiles(
      runStats(model, model, 0, {
        input: 8_100_000,
        cacheRead: 6_500_000,
        cacheWrite: null,
        output: 459_000,
        reasoning: null,
      }),
    );
    expect(tiles.map(tile => [tile.label, tile.figure, tile.note])).toEqual([
      ['Ideas tried', '2', '1 measured · 1 running'],
      ['Measurements', '1', '0 failed'],
      ['Agent time', workedWords(600), `of ${workedWords(1300)} running`],
      ['Tokens', '8.1M', '6.5M cached · 459k output'],
    ]);
    expect(statTiles(runStats(model, model, 0, null))[3]?.figure).toBe('Not reported');
  });
});
