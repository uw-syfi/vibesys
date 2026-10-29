import {describe, expect, test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {type DeliveryEvent, deliveryEnded} from './delivery.js';
import {EMPTY_DRAFT, STEER_MODES} from './steer.js';
import {
  type ControlOutcome,
  inForce,
  type SteerCardEvent,
  type SteerCardInput,
  steerCardMachine,
} from './steer-card.js';

const input: SteerCardInput = {
  id: 'proposal-1',
  origin: 'proposal',
  draft: {text: 'keep bf16', target: {kind: 'role', id: 'implementer'}, mode: 'standing'},
};

type Snapshot = ReturnType<typeof getInitialSnapshot<typeof steerCardMachine>>;

function start(): Snapshot {
  return getInitialSnapshot(steerCardMachine, input);
}

function step(
  snapshot: Snapshot,
  event: SteerCardEvent | {type: string; output?: unknown},
): Snapshot {
  return getNextSnapshot(steerCardMachine, snapshot, event as SteerCardEvent);
}

const ACK: ControlOutcome = {kind: 'acknowledged'};

function settled(
  snapshot: Snapshot,
  operation: 'send' | 'retract',
  output: ControlOutcome,
): Snapshot {
  return step(snapshot, {type: `xstate.done.actor.${operation}`, output});
}

function delivery(controlId: string, event: DeliveryEvent): SteerCardEvent {
  return {type: 'delivery', controlId, event};
}

function consistency({value, context}: Snapshot): string[] {
  const problems: string[] = [];
  const inDraft = value === 'draft';
  if (inDraft && context.controlId !== null) problems.push('a draft holds a control id');
  if (inDraft && context.delivery.kind !== 'sent') problems.push('a draft has a delivery');
  if (!inDraft && value !== 'dismissed' && context.controlId === null) {
    problems.push('a sent card has no control id');
  }
  if (typeof value === 'object' && value.sent === 'idle' && context.withdrawId !== null) {
    problems.push('an idle steer holds a withdrawal id');
  }
  return problems;
}

describe('steerCardMachine', () => {
  test('nothing is sent until Send: editing, retargeting and changing the mode stay in the draft', () => {
    let snapshot = start();
    snapshot = step(snapshot, {type: 'edit-text', text: 'keep fp8'});
    snapshot = step(snapshot, {type: 'set-target', target: {kind: 'run', id: null}});
    snapshot = step(snapshot, {type: 'set-mode', mode: 'interrupt'});
    expect(snapshot.value).toBe('draft');
    expect(snapshot.context.controlId).toBeNull();
    expect(snapshot.context.draft).toEqual({
      text: 'keep fp8',
      target: {kind: 'run', id: null},
      mode: 'interrupt',
    });
  });

  test('Send refuses an empty text', () => {
    let snapshot = step(start(), {type: 'edit-text', text: '  '});
    snapshot = step(snapshot, {type: 'send', controlId: 'c-1'});
    expect(snapshot.value).toBe('draft');
  });

  test('send, acknowledgment, delivery, withdrawal: the standing steer is in force, then withdrawn', () => {
    let snapshot = step(start(), {type: 'send', controlId: 'c-1'});
    expect(snapshot.value).toBe('sending');
    snapshot = settled(snapshot, 'send', ACK);
    expect(snapshot.value).toEqual({sent: 'idle'});
    expect(snapshot.context.delivery).toEqual({kind: 'sent'});
    snapshot = step(snapshot, delivery('c-1', {kind: 'queued'}));
    expect(snapshot.context.delivery).toEqual({kind: 'waiting'});
    snapshot = step(snapshot, delivery('c-1', {kind: 'delivered', idea: 3, where: null}));
    expect(inForce(snapshot.context)).toBe(true);
    snapshot = step(snapshot, {type: 'withdraw', controlId: 'w-1'});
    expect(snapshot.value).toEqual({sent: 'withdrawing'});
    snapshot = settled(snapshot, 'retract', ACK);
    expect(snapshot.value).toEqual({sent: 'withdrawRequested'});
    snapshot = step(snapshot, delivery('c-1', {kind: 'withdrawn'}));
    expect(snapshot.context.delivery).toEqual({kind: 'withdrawn'});
    expect(inForce(snapshot.context)).toBe(false);
  });

  test('a delivery report that beats the acknowledgment is kept', () => {
    let snapshot = step(start(), {type: 'send', controlId: 'c-1'});
    snapshot = step(snapshot, delivery('c-1', {kind: 'delivered', idea: 2, where: null}));
    snapshot = settled(snapshot, 'send', ACK);
    expect(snapshot.context.delivery).toEqual({kind: 'delivered', idea: 2, where: null});
  });

  test('a command the run did not take returns the card to its draft with the reason', () => {
    let snapshot = step(start(), {type: 'send', controlId: 'c-1'});
    snapshot = settled(snapshot, 'send', {kind: 'rejected', message: 'no such role'});
    expect(snapshot.value).toBe('draft');
    expect(snapshot.context.error).toBe('no such role');
    expect(snapshot.context.controlId).toBeNull();
    snapshot = step(snapshot, {type: 'send', controlId: 'c-2'});
    expect(snapshot.context.error).toBeNull();
    expect(snapshot.context.controlId).toBe('c-2');
  });

  test('a refused withdrawal leaves the steer in force with the reason', () => {
    let snapshot = settled(step(start(), {type: 'send', controlId: 'c-1'}), 'send', ACK);
    snapshot = step(snapshot, delivery('c-1', {kind: 'delivered', idea: 1, where: null}));
    snapshot = settled(step(snapshot, {type: 'withdraw', controlId: 'w-1'}), 'retract', ACK);
    snapshot = step(snapshot, delivery('w-1', {kind: 'refused', reason: 'not in force'}));
    expect(snapshot.value).toEqual({sent: 'idle'});
    expect(inForce(snapshot.context)).toBe(true);
    expect(snapshot.context.error).toBe('not in force');
  });

  test('Withdraw is only for a standing steer that is in force', () => {
    const once = {...input, draft: {...input.draft, mode: 'next_turn' as const}};
    let snapshot = getInitialSnapshot(steerCardMachine, once);
    snapshot = settled(step(snapshot, {type: 'send', controlId: 'c-1'}), 'send', ACK);
    snapshot = step(snapshot, delivery('c-1', {kind: 'delivered', idea: 1, where: null}));
    expect(step(snapshot, {type: 'withdraw', controlId: 'w-1'}).value).toEqual({sent: 'idle'});
    let waiting = settled(step(start(), {type: 'send', controlId: 'c-1'}), 'send', ACK);
    waiting = step(waiting, {type: 'withdraw', controlId: 'w-1'});
    expect(waiting.value).toEqual({sent: 'idle'});
  });

  test('a report for another control changes nothing', () => {
    const sent = settled(step(start(), {type: 'send', controlId: 'c-1'}), 'send', ACK);
    expect(
      step(sent, delivery('c-9', {kind: 'delivered', idea: 1, where: null})).context.delivery,
    ).toEqual({
      kind: 'sent',
    });
  });

  test('Dismiss ends the card', () => {
    expect(step(start(), {type: 'dismiss'}).status).toBe('done');
  });

  test('model: whatever the order of sends, outcomes and reports, the card stays consistent', () => {
    const controls = ['c-1', 'c-2', 'w-1'];
    checkMachine({
      machine: steerCardMachine,
      input,
      event: (random, live) =>
        random.pick([
          {type: 'edit-text', text: random.pick(['', 'go', 'keep bf16'])},
          {type: 'set-mode', mode: random.pick(STEER_MODES).value},
          {type: 'set-target', target: random.pick([EMPTY_DRAFT.target, input.draft.target])},
          {type: 'send', controlId: random.pick(controls)},
          {type: 'withdraw', controlId: random.pick(controls)},
          {type: 'dismiss'},
          ...(['queued', 'dropped', 'ended', 'withdrawn'] as const).map(kind =>
            delivery(random.pick(controls), {kind}),
          ),
          delivery(random.pick(controls), {kind: 'delivered', idea: random.int(5), where: null}),
          delivery(random.pick(controls), {kind: 'refused', reason: null}),
          ...live.operations.map(operation =>
            live.done(operation, random.bool() ? ACK : {kind: 'rejected', message: 'no'}),
          ),
          ...live.operations.map(operation => live.error(operation, new Error('lost'))),
        ]),
      invariants: consistency,
      also: (before, _event, after) => {
        // A delivery that ended never moves again, except that a send which did not go through
        // forgets what was reported on it and returns to the draft.
        if (deliveryEnded(before.context.delivery) && after.value !== 'draft') {
          expect(after.context.delivery).toEqual(before.context.delivery);
        }
      },
    });
  });
});

describe('a card on a run that cannot take a steer', () => {
  test('Send does nothing: the card keeps its draft and asks the run for nothing', () => {
    const readOnly = getInitialSnapshot(steerCardMachine, {...input, readOnly: true});
    const sent = step(readOnly, {type: 'send', controlId: 'c-1'});
    expect(sent.value).toBe('draft');
    expect(sent.context.controlId).toBeNull();
  });

  test('a run that ends under an open card turns Send off, and keeps the draft for a run that continues', () => {
    let snapshot = step(start(), {type: 'read-only', value: true});
    snapshot = step(snapshot, {type: 'edit-text', text: 'still editable'});
    expect(step(snapshot, {type: 'send', controlId: 'c-1'}).value).toBe('draft');
    expect(snapshot.context.draft.text).toBe('still editable');
    snapshot = step(snapshot, {type: 'read-only', value: false});
    expect(step(snapshot, {type: 'send', controlId: 'c-2'}).value).toBe('sending');
  });

  test('the typed refusal of a finished run is worded by the client', () => {
    let snapshot = step(start(), {type: 'send', controlId: 'c-1'});
    snapshot = settled(snapshot, 'send', {
      kind: 'rejected',
      message: 'This finished run is read-only',
      code: 'run_read_only',
    });
    expect(snapshot.value).toBe('draft');
    expect(snapshot.context.error).toBe('Read only');
  });
});
