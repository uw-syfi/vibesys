import {describe, expect, test} from 'bun:test';
import type {CandidateOutcome} from '@vibesys/run-state/candidates';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {modelOfShape} from './ideas-tree-fixtures.js';
import {
  chipPressed,
  IDEAS_FILTER_DEFAULT,
  type IdeasFilter,
  ideasView,
  noIdeasWords,
  pressChip,
} from './ideas-view.js';

const GROUPS: readonly CandidateOutcome[] = ['passed', 'running', 'failed'];

const MODEL = modelOfShape({
  parents: [null, 0, 1, 0, 3, 3, 5],
  outcomes: ['passed', 'passed', 'passed', 'passed', 'failed', 'passed', 'failed'],
  best: 2,
});

function randomFilter(random: Seeded): IdeasFilter {
  return {
    query: random.pick(['', 'idea', 'idea 4', 'zzzz']),
    passed: random.bool(),
    running: random.bool(),
    failed: random.bool(),
  };
}

describe('the filter chips', () => {
  test('a chip shows its own group, whatever else was chosen before', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      for (let n = 0; n < 30; n += 1) {
        const filter = {...randomFilter(random), query: ''};
        for (const group of GROUPS) {
          const pressed = pressChip(filter, group);
          // Pressing the chosen chip is the one press that shows every group (tested below).
          if (GROUPS.every(each => pressed[each])) continue;
          const rows = ideasView(MODEL, pressed).rows;
          expect(rows.every(row => row.outcome === group)).toBe(true);
          expect(rows.length).toBe(ideasView(MODEL, IDEAS_FILTER_DEFAULT).counts[group]);
        }
      }
    }
  });

  test('pressing the chosen chip again shows every group, and nothing is pressed then', () => {
    for (const group of GROUPS) {
      const chosen = pressChip(IDEAS_FILTER_DEFAULT, group);
      expect(GROUPS.filter(each => chipPressed(chosen, each))).toEqual([group]);
      const again = pressChip(chosen, group);
      expect(again).toEqual(IDEAS_FILTER_DEFAULT);
      expect(GROUPS.some(each => chipPressed(again, each))).toBe(false);
    }
  });

  test('a press keeps the search', () => {
    for (const group of GROUPS) {
      expect(pressChip({...IDEAS_FILTER_DEFAULT, query: 'idea 4'}, group).query).toBe('idea 4');
    }
  });
});

describe('an empty list', () => {
  test('says what matched nothing, in the reader’s words', () => {
    expect(noIdeasWords({...IDEAS_FILTER_DEFAULT, query: ' zzzz '}).title).toBe(
      'No ideas match “zzzz”',
    );
    expect(noIdeasWords(pressChip(IDEAS_FILTER_DEFAULT, 'running')).title).toBe('No running ideas');
  });
});
