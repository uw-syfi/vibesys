import {it} from 'node:test';
import {validateRunEvent} from './index.js';
import {expect} from './test-support/expect.js';

function event(data: unknown): Record<string, unknown> {
  return {
    protocol_version: 1,
    sequence: 1,
    timestamp: '2026-09-27T00:00:00Z',
    type: 'agent_execution_started',
    data,
  };
}

it('validates every declared field of a known event-data member', () => {
  expect(() =>
    validateRunEvent(
      event({
        kind: 'agent_execution_started',
        stage: 'implement',
      }),
    ),
  ).toThrow('Invalid server run event.data: activity must be present');

  expect(() =>
    validateRunEvent(
      event({
        kind: 'agent_execution_started',
        stage: 'implement',
        activity: {
          kind: 'agent_execution_activity_changed',
          summary: 'working',
        },
      }),
    ),
  ).toThrow('Invalid server run event.data.activity: mode must be present');
});

it('returns a fully valid known member through the public package interface', () => {
  const value = event({
    kind: 'agent_execution_started',
    stage: 'implement',
    activity: {
      kind: 'agent_execution_activity_changed',
      mode: 'thinking',
      summary: 'working',
    },
  });

  expect(validateRunEvent(value)).toBe(value);
});

it('keeps unknown tagged-union members and fields forward compatible', () => {
  const value = {
    ...event({kind: 'future-data-kind', shape: {nested: true}}),
    type: 'future-event-type',
    status: 'future-status',
    future_field: true,
  };

  expect(validateRunEvent(value)).toBe(value);
});

it('enforces generated scalar constraints on direct unknown input', () => {
  expect(() => validateRunEvent({...event(null), sequence: -1})).toThrow(
    'Invalid server run event.sequence: must be at least 0',
  );
  expect(() => validateRunEvent({...event(null), timestamp: 'not-a-date'})).toThrow(
    'Invalid server run event.timestamp: must be a date-time',
  );
});
