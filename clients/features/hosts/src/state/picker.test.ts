import {describe, expect, test} from 'bun:test';
import {checkModel, Seeded} from '@vibesys/core-state/testing';
import type {WelcomeResult} from '@vibesys/ipc';
import {
  filterHosts,
  moveSelection,
  PICKER_CLOSED,
  type PickerAction,
  type PickerState,
  pickerChoices,
  pickerInvariants,
  pickerStep,
} from './picker.js';

describe('filterHosts', () => {
  const aliases = ['gpu-box', 'cluster.example.org', 'GPU-2'];

  test('an empty query lists every alias', () => {
    expect(filterHosts(aliases, '  ')).toEqual(aliases.map(alias => ({alias, typed: false})));
  });

  test('matches case-insensitively and offers a typed destination last', () => {
    expect(filterHosts(aliases, 'gpu')).toEqual([
      {alias: 'gpu-box', typed: false},
      {alias: 'GPU-2', typed: false},
      {alias: 'gpu', typed: true},
    ]);
    expect(filterHosts(aliases, 'me@node-1')).toEqual([{alias: 'me@node-1', typed: true}]);
    expect(filterHosts(aliases, 'gpu-box')).toEqual([{alias: 'gpu-box', typed: false}]);
  });

  test('never offers a destination ssh would read as an option or that has spaces', () => {
    expect(filterHosts(aliases, '-oProxyCommand=x')).toEqual([]);
    expect(filterHosts(aliases, 'a b')).toEqual([]);
  });
});

describe('moveSelection', () => {
  test('stays in range and wraps, from any start', () => {
    const random = new Seeded(42);
    for (let round = 0; round < 1000; round += 1) {
      const length = random.int(6);
      const index = random.int(length + 2) - 1;
      const delta = random.bool() ? 1 : -1;
      const next = moveSelection(index, delta, length);
      if (length === 0) expect(next).toBe(-1);
      else {
        expect(next).toBeGreaterThanOrEqual(0);
        expect(next).toBeLessThan(length);
      }
    }
    expect(moveSelection(-1, 1, 3)).toBe(0);
    expect(moveSelection(-1, -1, 3)).toBe(2);
    expect(moveSelection(2, 1, 3)).toBe(0);
  });
});

function failed<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

function randomPickerAction(random: Seeded, issued: readonly number[]): PickerAction {
  return random.pick<PickerAction>([
    {type: 'open'},
    {type: 'close'},
    {type: 'query', value: random.pick(['', 'gpu', 'me@box', '-oProxy', 'zzz'])},
    {type: 'move', delta: random.pick([1, -1])},
    {type: 'choose'},
    {type: 'choose', index: random.int(4)},
    {
      type: 'aliases-reply',
      token: random.pick(issued),
      result: random.bool()
        ? {ok: true, value: ['gpu-box', 'gpu-2', 'login'].slice(0, random.int(4))}
        : failed('no config'),
    },
  ]);
}

describe('host picker', () => {
  test('model: the selection always names a listed row; choosing shows a listed host', () => {
    checkModel({
      initial: PICKER_CLOSED,
      step: pickerStep,
      action: randomPickerAction,
      issues: request => (request.kind === 'aliases' ? request.token : null),
      awaited: (state: PickerState) => ({
        aliases:
          state.kind === 'open' && state.aliases.kind === 'loading' ? state.aliases.token : null,
      }),
      reply: action =>
        action.type === 'aliases-reply' ? {slot: 'aliases', token: action.token} : null,
      invariants: pickerInvariants,
      also: (before, _action, after, requests) => {
        for (const request of requests) {
          if (request.kind !== 'show-host') continue;
          expect(after.kind).toBe('closed');
          const listed = pickerChoices(before).map(choice => `ssh:${choice.alias}`);
          expect(listed).toContain(request.host);
        }
      },
    });
  });
});
