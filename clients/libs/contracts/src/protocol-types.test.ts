import {it} from 'node:test';
import type {EventBatchMessage} from './index.js';
import {expect} from './test-support/expect.js';

const ordinary: EventBatchMessage = {type: 'event_batch', events: []};
const explicitlyNullOrdinary: EventBatchMessage = {
  type: 'event_batch',
  events: [],
  active_executions_checkpoint_id: null,
};
const deferred: EventBatchMessage = {
  type: 'event_batch',
  events: [],
  active_executions: null,
  active_executions_checkpoint_id: 'a'.repeat(64),
};

// @ts-expect-error A null checkpoint is deferred and must carry its identity.
const unidentifiedDeferred: EventBatchMessage = {
  type: 'event_batch',
  events: [],
  active_executions: null,
};

// @ts-expect-error A deferred checkpoint identity must be a non-null string.
const nullIdentifiedDeferred: EventBatchMessage = {
  type: 'event_batch',
  events: [],
  active_executions: null,
  active_executions_checkpoint_id: null,
};

// @ts-expect-error An ordinary authoritative list cannot advertise a deferred identity.
const identifiedOrdinary: EventBatchMessage = {
  type: 'event_batch',
  events: [],
  active_executions: [],
  active_executions_checkpoint_id: 'a'.repeat(64),
};

it('generates distinct ordinary and deferred event-batch arms', () => {
  expect(ordinary.active_executions).toBeUndefined();
  expect(explicitlyNullOrdinary.active_executions_checkpoint_id).toBeNull();
  expect(deferred.active_executions_checkpoint_id?.length).toBe(64);
  void unidentifiedDeferred;
  void nullIdentifiedDeferred;
  void identifiedOrdinary;
});
