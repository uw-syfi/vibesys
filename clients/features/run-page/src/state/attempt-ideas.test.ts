import {expect, test} from 'bun:test';
import {ideaOfAttempt} from './attempt-ideas.js';
import {modelOfShape} from './ideas-tree-fixtures.js';

test('an attempt belongs to the idea that holds it, counted from one in the order ideas began', () => {
  const {candidates} = modelOfShape({parents: [null, 0, 0]}).candidates;
  const ideaOf = ideaOfAttempt(candidates);
  candidates.forEach((idea, index) => {
    expect(ideaOf(idea.id)).toBe(index + 1);
    for (const attempt of idea.attempts) expect(ideaOf(attempt.span)).toBe(index + 1);
  });
  expect(ideaOf('no such attempt')).toBeNull();
});
