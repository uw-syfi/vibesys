import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import type {Candidate} from '@vibesys/run-state/candidates';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {implementerOutcomes} from './idea-outcomes.js';
import {attempt, idea, modelOf} from './timeline-fixtures.js';

/**
 * What each idea's implementer reported, folded from the run's events: the choice its structured
 * reply made as an outcome, and its summary; the latest attempt's reply speaks for the idea.
 */

const SCHEMA = {
  properties: {
    summary: {type: 'string'},
    outcome: {enum: ['done', 'blocked'], type: 'string'},
  },
  required: ['summary'],
  type: 'object',
};

/** The implementer's reply to the turn `span`, as the run emits it when the turn finishes. */
function reply(
  sequence: number,
  span: string,
  value: Readonly<Record<string, unknown>>,
  schema: unknown = SCHEMA,
): RunEvent {
  return event(sequence, 'agent_execution_finished', {
    invocation_id: `inv-${span}`,
    agent_kind: 'implementer',
    span_id: span,
    data: {
      kind: 'agent_execution_finished',
      result: '{}',
      reply: {kind: 'ImplementerReply', json_schema: schema, value},
    },
  } as never);
}

const turnOf = (candidate: Candidate, attemptAt: number): string =>
  candidate.attempts[attemptAt]?.steps.find(step => step.role === 'implementer')?.span ?? '';

describe('the implementer outcome of an idea', () => {
  test('is the reply’s outcome and summary, with the tone its word carries', () => {
    const ideas = [idea('a', 'passed', 0, 60), idea('b', 'failed', 100, 60)];
    const core = reduceEventBatch(initialCoreState(), [
      reply(1, turnOf(ideas[0] as Candidate, 0), {
        summary: 'Padded the counters.',
        outcome: 'done',
      }),
      reply(2, turnOf(ideas[1] as Candidate, 0), {summary: 'Could not build.', outcome: 'blocked'}),
    ]);
    const found = implementerOutcomes(core, modelOf(ideas).candidates);
    expect(found.get('a')).toEqual({
      outcome: {label: 'done', tone: 'neutral'},
      summary: 'Padded the counters.',
    });
    expect(found.get('b')?.outcome).toEqual({label: 'blocked', tone: 'err'});
    expect(found.get('b')?.summary).toBe('Could not build.');
  });

  test('reads a reply whose choice is named for its hypothesis', () => {
    const ideas = [idea('a', 'passed', 0, 60)];
    const schema = {
      properties: {
        summary: {type: 'string'},
        hypothesis_outcome: {enum: ['nominated', 'supported'], type: 'string'},
        candidate_disposition: {enum: ['discard', 'unassessed'], type: 'string'},
      },
      type: 'object',
    };
    const core = reduceEventBatch(initialCoreState(), [
      reply(
        1,
        turnOf(ideas[0] as Candidate, 0),
        {summary: 'Done.', hypothesis_outcome: 'supported', candidate_disposition: 'unassessed'},
        schema,
      ),
    ]);
    expect(implementerOutcomes(core, modelOf(ideas).candidates).get('a')?.outcome).toEqual({
      label: 'supported',
      tone: 'ok',
    });
  });

  test('is the latest attempt’s reply, and absent for an idea whose implementer said nothing', () => {
    const base = idea('a', 'passed', 0, 60);
    const first = attempt('a1', 'failed', 0, 30);
    const second = attempt('a2', 'passed', 40, 30);
    const twice = {...base, attempts: [first, second]};
    const silent = idea('s', 'running', 100, 0);
    const core = reduceEventBatch(initialCoreState(), [
      reply(1, turnOf(twice, 1), {summary: 'Second try.', outcome: 'done'}),
      reply(2, turnOf(twice, 0), {summary: 'First try.', outcome: 'blocked'}),
    ]);
    const found = implementerOutcomes(core, modelOf([twice, silent]).candidates);
    // The reply of the later attempt wins whatever order the replies arrived in.
    expect(found.get('a')?.summary).toBe('Second try.');
    expect(found.has('s')).toBe(false);
  });

  test('is empty before the run fold reaches the window', () => {
    expect(implementerOutcomes(null, modelOf([idea('a', 'passed', 0, 60)]).candidates).size).toBe(
      0,
    );
  });

  test('over any run, each idea reads its own latest reply and nothing else', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const ideas = Array.from({length: 1 + random.int(8)}, (_, at) =>
        idea(`i${at}`, 'passed', at * 100, 60),
      );
      const events: RunEvent[] = [];
      const expected = new Map<string, string>();
      for (const item of ideas) {
        if (random.bool()) continue;
        const summary = `summary of ${item.id}`;
        events.push(reply(events.length + 1, turnOf(item, 0), {summary, outcome: 'done'}));
        expected.set(item.id, summary);
      }
      const found = implementerOutcomes(
        reduceEventBatch(initialCoreState(), events),
        modelOf(ideas).candidates,
      );
      expect(new Map([...found].map(([id, each]) => [id, each.summary]))).toEqual(expected);
    }
  });
});
