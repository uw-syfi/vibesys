import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {type CardValue, cardView} from './card-view.js';
import type {DeliveryEvent} from './delivery.js';
import {foldDelivery, SENT} from './delivery.js';
import type {SteerDraft} from './steer.js';
import type {SteerCardContext} from './steer-card.js';

const DRAFT: SteerDraft = {
  text: 'keep bf16',
  target: {kind: 'role', id: 'implementer'},
  mode: 'standing',
};

function context(patch: Partial<SteerCardContext> = {}): SteerCardContext {
  return {
    id: 'p-1',
    origin: 'proposal',
    draft: DRAFT,
    controlId: 'c-1',
    withdrawId: null,
    delivery: SENT,
    readOnly: false,
    error: null,
    ...patch,
  };
}

const SENT_IDLE: CardValue = {sent: 'idle'};

describe('cardView', () => {
  test('a card on a run that ended offers no Send, with its reason as a word', () => {
    const view = cardView(context({readOnly: true, controlId: null}), 'draft');
    expect(view.sendBlocked).toBe('Read only');
  });

  test('every delivery state has a status word, and a standing steer in force offers Withdraw', () => {
    const states: [DeliveryEvent[], string, boolean][] = [
      [[], 'Sent', false],
      [[{kind: 'queued'}], 'Waiting', false],
      [[{kind: 'delivered', idea: 4, where: 'implementer'}], 'In force · idea 4', true],
      [
        [{kind: 'delivered', idea: null, where: 'implementer'}],
        'In force · implementer · every turn',
        true,
      ],
      [[{kind: 'dropped'}], 'Dropped', false],
      [[{kind: 'delivered', idea: 2, where: null}, {kind: 'ended'}], 'Ended', false],
      [[{kind: 'withdrawn'}], 'Withdrawn', false],
      [[{kind: 'refused', reason: 'no such role'}], 'Refused: no such role', false],
    ];
    for (const [events, label, withdrawable] of states) {
      const delivery = events.reduce(foldDelivery, SENT);
      const view = cardView(context({delivery}), SENT_IDLE);
      expect(view.status?.label).toBe(label);
      expect(view.withdrawable).toBe(withdrawable);
    }
  });

  test('a steer for one turn says Reached, not In force', () => {
    const delivery = foldDelivery(SENT, {kind: 'delivered', idea: 3, where: null});
    const view = cardView(context({delivery, draft: {...DRAFT, mode: 'next_turn'}}), SENT_IDLE);
    expect(view.status?.label).toBe('Reached · idea 3');
    expect(view.withdrawable).toBe(false);
  });

  test('phases: a draft has no status, sending and withdrawing say so', () => {
    expect(cardView(context(), 'draft').status).toBeNull();
    expect(cardView(context(), 'sending').status?.label).toBe('Sending');
    expect(cardView(context(), {sent: 'withdrawing'}).status?.label).toBe('Withdrawing');
    expect(cardView(context(), {sent: 'withdrawRequested'}).phase).toBe('withdrawing');
    expect(cardView(context(), 'dismissed').phase).toBe('dismissed');
  });

  test('a withdrawal in flight gives way to the ending the server reported', () => {
    const delivery = [{kind: 'delivered', idea: 1, where: null}, {kind: 'withdrawn'}].reduce(
      (state, each) => foldDelivery(state, each as DeliveryEvent),
      SENT,
    );
    const view = cardView(context({delivery}), {sent: 'withdrawRequested'});
    expect(view.status?.label).toBe('Withdrawn');
    expect(view.phase).toBe('sent');
  });

  test('Send is blocked with a word when the draft cannot go', () => {
    expect(cardView(context(), 'draft').sendBlocked).toBeNull();
    expect(cardView(context({draft: {...DRAFT, text: ' '}}), 'draft').sendBlocked).toBe('Empty');
  });

  test('whatever events arrive, a view always has a status once sent', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      let delivery = SENT;
      for (let step = 0; step < 6; step += 1) {
        delivery = foldDelivery(
          delivery,
          random.pick<DeliveryEvent>([
            {kind: 'queued'},
            {kind: 'delivered', idea: random.int(5), where: null},
            {kind: 'dropped'},
            {kind: 'ended'},
            {kind: 'withdrawn'},
            {kind: 'refused', reason: null},
          ]),
        );
        const view = cardView(
          context({
            delivery,
            draft: {...DRAFT, mode: random.pick(['next_turn', 'standing', 'interrupt'] as const)},
          }),
          SENT_IDLE,
        );
        expect(view.status?.label.length).toBeGreaterThan(0);
      }
    }
  });

  test('a delivery with no named place says where the draft was aimed', () => {
    const delivery = foldDelivery(SENT, {kind: 'delivered', idea: null, where: null});
    expect(cardView(context({delivery}), SENT_IDLE).status?.label).toBe(
      'In force · Builder · every turn',
    );
    const once = {...DRAFT, mode: 'next_turn'} as const;
    expect(cardView(context({delivery, draft: once}), SENT_IDLE).status?.label).toBe(
      'Reached · Builder',
    );
  });
});
