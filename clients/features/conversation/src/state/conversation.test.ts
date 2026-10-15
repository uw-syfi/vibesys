import {expect, test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {conversationMachine, conversationView} from './index.js';

test('model: one pending request per intent, trimmed requests, and no duplicate sends', () => {
  checkMachine({
    machine: conversationMachine,
    input: {connected: true, steerAvailable: true},
    event: (random, live) =>
      random.pick([
        {type: 'open', open: random.bool()},
        {type: 'intent', intent: random.pick(['ask', 'steer'] as const)},
        {type: 'draft', text: random.pick(['', '   ', ' question ', 'guidance'])},
        {
          type: 'availability',
          availability: {connected: random.bool(), steerAvailable: random.bool()},
        },
        {type: 'submit', transcriptStart: random.int(6)},
        ...live.operations.flatMap(operation => [
          live.done(operation, 'answer'),
          live.error(operation, new Error('failure')),
        ]),
      ]),
    invariants: snapshot => {
      const view = conversationView(snapshot);
      const errors: string[] = [];
      for (const intent of ['ask', 'steer'] as const) {
        const pending = view.submissions.filter(
          item => item.intent === intent && item.status === 'pending',
        );
        if (pending.length !== (snapshot.matches({[intent]: 'sending'}) ? 1 : 0))
          errors.push(`pending ${intent}`);
      }
      if (view.submissions.some(item => item.text.length === 0 || item.text !== item.text.trim()))
        errors.push('untrimmed request');
      if (new Set(view.submissions.map(item => item.id)).size !== view.submissions.length)
        errors.push('duplicate ids');
      return errors;
    },
  });
});

test('Ask remains available on an ended run; reconnect and mode changes keep drafts', () => {
  let state = getInitialSnapshot(conversationMachine, {connected: false, steerAvailable: false});
  state = getNextSnapshot(conversationMachine, state, {type: 'draft', text: 'ask draft'});
  state = getNextSnapshot(conversationMachine, state, {type: 'submit', transcriptStart: 0});
  expect(conversationView(state).submissions).toHaveLength(0);
  state = getNextSnapshot(conversationMachine, state, {
    type: 'availability',
    availability: {connected: true, steerAvailable: false},
  });
  state = getNextSnapshot(conversationMachine, state, {type: 'intent', intent: 'steer'});
  state = getNextSnapshot(conversationMachine, state, {type: 'draft', text: 'steer draft'});
  state = getNextSnapshot(conversationMachine, state, {type: 'submit', transcriptStart: 0});
  expect(conversationView(state).submissions).toHaveLength(0);
  state = getNextSnapshot(conversationMachine, state, {type: 'intent', intent: 'ask'});
  state = getNextSnapshot(conversationMachine, state, {type: 'submit', transcriptStart: 0});
  expect(conversationView(state)).toMatchObject({
    drafts: {ask: '', steer: 'steer draft'},
    submissions: [{text: 'ask draft', status: 'pending', intent: 'ask'}],
  });
});
