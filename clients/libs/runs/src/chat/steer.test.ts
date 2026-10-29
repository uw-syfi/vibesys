import {expect, test} from 'bun:test';
import {HostCallRequestSchema, SteerTargetKind, validatedFromBinary} from '@vibesys/proto';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  addressedIntent,
  configuredHostCall,
  hostIntentRequest,
  runReference,
} from '../intents/index.js';
import {type ChatControl, chatControlIntent} from './index.js';

const KINDS = [undefined, 'run', 'role', 'attempt', 'item', 'unknown'] as const;
const MODES = [undefined, 'next_turn', 'standing', 'interrupt'] as const;

function control(random: Seeded): ChatControl {
  if (random.int(4) === 0) return {action: 'withdraw', withdraws: `control-${random.int(1000)}`};
  const kind = random.pick(KINDS);
  const id = random.pick([undefined, null, '', `id-${random.int(100)}`]);
  return {
    action: 'steer',
    text: `steer ${random.int(1000)}`,
    mode: random.pick(MODES),
    target: kind === undefined ? undefined : {kind, id},
  };
}

/** A target needs an id exactly when it is not the whole run; every other input is refused up front. */
const needsId = (control: ChatControl): boolean =>
  control.action === 'steer' && ['role', 'attempt', 'item'].includes(control.target?.kind ?? '');

test('every steer a card can send is admitted by the protocol codec or refused before it is built', () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    for (let index = 0; index < 60; index++) {
      const request = control(random);
      const idMissing =
        needsId(request) && !(request as {target?: {id?: string | null}}).target?.id;
      if (idMissing) {
        expect(() => chatControlIntent(request)).toThrow('needs an id');
        continue;
      }
      const intent = addressedIntent(
        runReference('/project', 'run-a'),
        `r-${index}`,
        1n,
        chatControlIntent(request),
      );
      const sent = configuredHostCall(hostIntentRequest(intent), '/checkout');
      expect(validatedFromBinary(HostCallRequestSchema, sent).method.case).toBe('postIntent');
    }
  }
});

test('the whole run names no target id', () => {
  const intent = chatControlIntent({action: 'steer', text: 'go', target: {kind: 'run', id: ''}});
  expect(intent.case === 'steer' && intent.value.target).toEqual({kind: SteerTargetKind.RUN});
});
