import {describe, expect, test} from 'bun:test';
import type {Candidate} from '@vibesys/run-state/candidates';
import {figureOf, outcomeWords} from './idea-words.js';
import {IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import type {RunModel} from './run-model.js';
import {idea, modelOf as treeOf} from './timeline-fixtures.js';

const modelOf = (candidates: readonly Candidate[], best?: string): RunModel =>
  ({
    ...treeOf(candidates, best === undefined ? null : {candidate: best, adopted: false}),
    inputGates: [],
    changed: new Set<string>(),
  }) as unknown as RunModel;

/** The Ideas rows' words: failed rows read "failed · reason", a planning idea names no base. */

const REASONS = ['turned_down', 'not_measured', 'no_design', 'not_opened'];

function failedFor(reason: string | null): Candidate {
  return {...idea('f', 'failed', 0, 60), reason, measurement: null};
}

describe('a failed idea', () => {
  test('reads "failed · <reason>" on its row and in its state, with no underscores, and its own result is no reason', () => {
    for (const reason of REASONS) {
      const item = failedFor(reason);
      const words = reason.replaceAll('_', ' ');
      const said = reason === 'turned_down' ? 'Failed' : `Failed · ${words}`;
      expect(figureOf(item).figure).toBe(said);
      expect(outcomeWords(item)).toBe(said.toLowerCase());
      const view = ideasView(modelOf([item]), IDEAS_FILTER_DEFAULT);
      expect(view.rows[0]?.state.label).toBe(said);
      expect(view.rows[0]?.figure).toBe(said);
    }
  });

  test('says only "failed" when the run gave no reason', () => {
    expect(figureOf(failedFor(null)).figure).toBe('Failed');
  });
});

describe('the bar column', () => {
  test('every row holds it, so a row with no share leaves an empty track under "vs best"', () => {
    const measured = idea('m', 'passed', 0, 60);
    const view = ideasView(
      modelOf([measured, failedFor('not_measured')], 'm'),
      IDEAS_FILTER_DEFAULT,
    );
    expect(view.rows.map(row => row.bar.value > 0)).toEqual([false, true]);
    expect(view.rows).toHaveLength(2);
  });
});

describe('an idea still being planned', () => {
  const planning: Candidate = {...idea('round 5', 'running', 0, 0), title: 'round 5'};

  test('has no base and no round-number title until an agent names it', () => {
    const row = ideasView(modelOf([planning]), IDEAS_FILTER_DEFAULT).rows[0];
    expect(row?.title).toBe('planning…');
    expect(row?.trail).toEqual({kind: 'idea', label: '…', parent: null});
  });

  test('reads its base once the idea is named', () => {
    const named: Candidate = {...planning, title: 'Pad the counters', idea: 'Pad them.'};
    const row = ideasView(modelOf([named]), IDEAS_FILTER_DEFAULT).rows[0];
    expect(row?.title).toBe('Pad the counters');
    expect(row?.trail.label).toBe('input');
  });
});
