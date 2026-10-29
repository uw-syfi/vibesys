import {it} from 'node:test';
import {validateAgentReply, validateRunEvent} from './index.js';
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

it('accepts only a boolean event-payload truncation marker', () => {
  const value = {...event(null), truncated: true};

  expect(validateRunEvent(value)).toBe(value);
  expect(() => validateRunEvent({...event(null), truncated: 'true'})).toThrow(
    'Invalid server run event.truncated: must be a boolean',
  );
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

it('rejects nonportable chat thread identities from the generated schema', () => {
  for (const threadId of [
    '',
    '../outside',
    'nested/thread',
    String.raw`nested\thread`,
    'C:drive',
    'con',
    'nul.txt',
    'trailing.',
    'trailing ',
    'trailing\n',
    'a'.repeat(129),
  ]) {
    expect(() => validateRunEvent({...event(null), chat_thread_id: threadId})).toThrow(
      'Invalid server run event.chat_thread_id',
    );
  }

  const value = {...event(null), chat_thread_id: 'thread-1.name'};
  expect(validateRunEvent(value)).toBe(value);
});

it('retained typed replies reject malformed fields and unknown routing keys', () => {
  const reply = {kind: 'Result', json_schema: {type: 'object'}, value: {answer: 'yes'}};
  expect(validateAgentReply(reply)).toEqual(reply);
  for (const invalid of [
    {...reply, kind: ''},
    {...reply, json_schema: []},
    {...reply, value: null},
    {...reply, unknown: true},
  ]) {
    expect(() => validateAgentReply(invalid)).toThrow();
  }
});
