import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  draftProblem,
  EMPTY_DRAFT,
  STEER_MODES,
  type SteerDraft,
  steerRequest,
  TARGET_KINDS,
  withdrawRequest,
} from './steer.js';

function randomDraft(random: Seeded): SteerDraft {
  const kind = random.pick(TARGET_KINDS).value;
  return {
    text: random.pick(['', '  ', 'keep bf16', ' trim me ']),
    target: {kind, id: kind === 'run' ? null : random.pick([null, '', ' ', 'implementer', 'a-3'])},
    mode: random.pick(STEER_MODES).value,
  };
}

describe('steer requests', () => {
  test('a draft with no problem becomes a request that says exactly what the card shows', () => {
    for (const seed of SEEDS) {
      const draft = randomDraft(new Seeded(seed));
      if (draftProblem(draft) !== null) continue;
      const request = steerRequest('control-1', draft);
      expect(request.action).toBe('steer');
      expect(request.control_id).toBe('control-1');
      expect(request.text).toBe(draft.text.trim());
      expect(request.mode).toBe(draft.mode);
      expect(request.target?.kind).toBe(draft.target.kind);
      if (draft.target.kind === 'run') expect(request.target?.id).toBeUndefined();
      else expect(request.target?.id).toBe((draft.target.id ?? '').trim());
    }
  });

  test('an empty text or a missing target is a problem, and nothing else is', () => {
    expect(draftProblem(EMPTY_DRAFT)).toBe('Empty');
    expect(draftProblem({...EMPTY_DRAFT, text: 'go'})).toBeNull();
    expect(draftProblem({...EMPTY_DRAFT, text: 'go', target: {kind: 'role', id: ' '}})).toBe(
      'No target',
    );
  });

  test('a withdrawal names the steer it ends', () => {
    expect(withdrawRequest('w-1', 'control-1')).toEqual({
      control_id: 'w-1',
      action: 'withdraw',
      withdraws: 'control-1',
    });
  });
});
