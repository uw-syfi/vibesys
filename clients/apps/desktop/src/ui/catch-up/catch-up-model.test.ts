import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {AgentRole, ReportSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {windowFactsOf} from '../../testing/orchestrator/check-ins.js';
import {busyFacts} from '../../testing/orchestrator/index.js';
import {type CatchUpWords, reportWords, tilesOf} from './catch-up-model.js';

const words: CatchUpWords = {role: role => `role ${AgentRole[role]}`};

describe('the tiles of a window', () => {
  it('are the facts the host froze, said in words', () => {
    const tiles = tilesOf(busyFacts(0), words);
    expect(tiles.agents).toEqual({started: '4', ended: '3'});
    expect(tiles.jobsMeasured).toBe('7');
    expect(tiles.ideas).toEqual({created: '2', changed: '1'});
    expect(tiles.spend.total).toBe('$13.15');
    expect(tiles.spend.turns).toBe('16');
    expect(tiles.spend.unpriced).toBe('2');
    expect(tiles.spend.byType.map(row => [row.name, row.figures])).toEqual([
      ['role LEAD', ['4', '$1.25']],
      ['role PLANNER', ['2', '$0.50']],
      ['role WORKER', ['9', '$11.40']],
      ['role JUDGE', ['1', null]],
    ]);
    // A type with no priced turn has no bar and says why, never a cost of nothing.
    expect(tiles.spend.byType.map(row => row.value)).toEqual([1.25, 0.5, 11.4, null]);
    expect(tiles.spend.byType.at(-1)?.note).toBe('No price');
    expect(tiles.spend.byType.at(2)?.note).toBe('1 unpriced');
    expect(tiles.objectives).toEqual([
      {
        metric: 'throughput',
        unit: 'tok/s',
        direction: 'maximize',
        start: '1.84',
        end: '2.31',
        source: 'job-7 · job-7abcdef',
      },
      {
        metric: 'p99 latency',
        unit: '',
        direction: 'minimize',
        start: null,
        end: '118',
        source: 'job-6 · job-6abcdef',
      },
    ]);
  });

  it('keeps the order of the host for types and objectives, and the direction it declared', () => {
    const tiles = tilesOf(busyFacts(0), words);
    expect(tiles.objectives.map(objective => objective.direction)).toEqual([
      'maximize',
      'minimize',
    ]);
  });

  it('say how many turns have no price, and leave the cost absent when none had one', () => {
    fc.assert(
      fc.property(
        fc.nat({max: 500}),
        fc.nat({max: 500}),
        fc.option(fc.double({min: 0, max: 1e4, noNaN: true}), {nil: undefined}),
        (turns, unpriced, costUsd) => {
          const tiles = tilesOf(
            windowFactsOf({turns: Math.max(1, turns, unpriced), unpricedTurns: unpriced, costUsd}),
            words,
          );
          // The total is the host's figure or nothing: a window with no price is not a free one.
          expect(tiles.spend.total === null).toBe(costUsd === undefined);
          if (tiles.spend.total !== null) expect(tiles.spend.total).toMatch(/^[$<]/);
          expect(tiles.spend.unpriced === null).toBe(unpriced === 0);
        },
      ),
      {numRuns: 100},
    );
  });
});

describe('a report as the page reads it', () => {
  it('is its headline and the parts the lead filled in, in a fixed order', () => {
    const report = create(ReportSchema, {
      headline: 'Best moved to 2.31x',
      openForUser: 'Pick a cache size',
      changed: 'Three ideas were rejected',
      decisions: '   ',
    });
    expect(reportWords(report)).toEqual({
      headline: 'Best moved to 2.31x',
      parts: [
        {label: 'Changed', text: 'Three ideas were rejected'},
        {label: 'Needs you', text: 'Pick a cache size'},
      ],
    });
  });
});
