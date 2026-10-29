import {describe, expect, test} from 'bun:test';
import {roleName} from '@vibesys/contracts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  draftProblem,
  EMPTY_DRAFT,
  parseTargetValue,
  runRoles,
  STEER_MODES,
  type SteerDraft,
  steerRequest,
  targetOptions,
  targetValue,
  withdrawRequest,
} from './steer.js';

function randomDraft(random: Seeded): SteerDraft {
  const kind = random.pick(['run', 'role', 'attempt', 'item'] as const);
  return {
    text: random.pick(['', '  ', 'keep bf16', ' trim me ']),
    target: {kind, id: kind === 'run' ? null : random.pick([null, '', ' ', 'implementer', 'a-3'])},
    mode: random.pick(STEER_MODES).value,
  };
}

describe('steer targets', () => {
  test('the options are the whole run, then each role by name, and always include the draft target', () => {
    const roles = ['orchestrator', 'perf-evaluator'];
    expect(targetOptions(roles, {kind: 'run', id: null})).toEqual([
      {value: 'run', label: 'Whole run'},
      {value: 'role:orchestrator', label: roleName('orchestrator')},
      {value: 'role:perf-evaluator', label: 'Perf evaluator'},
    ]);
    const attempt = targetOptions(roles, {kind: 'attempt', id: 'a-3'});
    expect(attempt.at(-1)).toEqual({value: 'attempt:a-3', label: 'Attempt a-3'});
    expect(attempt).toHaveLength(4);
  });

  test('a target round-trips through its select value, whatever its id holds', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const kind = random.pick(['role', 'attempt', 'item'] as const);
      const target = {kind, id: random.pick(['implementer', 'a:b', 'x y', ''])};
      expect(parseTargetValue(targetValue(target))).toEqual(target);
    }
    expect(parseTargetValue('run')).toEqual({kind: 'run', id: null});
    expect(parseTargetValue('nonsense')).toEqual({kind: 'run', id: null});
  });
});

describe('run roles', () => {
  test('advertised roles first, then the others seen, none twice', () => {
    expect(runRoles(['orchestrator', 'judge'], ['implementer', 'judge'])).toEqual([
      'orchestrator',
      'judge',
      'implementer',
    ]);
    expect(runRoles(null, ['judge', 'judge'])).toEqual(['judge']);
    expect(runRoles(null, [])).toEqual([]);
  });
});

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
