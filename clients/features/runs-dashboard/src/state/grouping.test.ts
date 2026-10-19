import {describe, expect, test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {GROUP_BYS, type GroupBy, groupingMachine} from './index.js';

describe('groupingMachine', () => {
  test('groups by status until told otherwise', () => {
    expect(getInitialSnapshot(groupingMachine).context.by).toBe('status');
  });

  test('takes the grouping it is given, and the same one again changes nothing', () => {
    for (const by of GROUP_BYS) {
      const once = getNextSnapshot(groupingMachine, getInitialSnapshot(groupingMachine), {
        type: 'group',
        by,
      });
      const twice = getNextSnapshot(groupingMachine, once, {type: 'group', by});
      expect(twice.context.by).toBe(by);
    }
  });

  test('model: whatever is chosen, the grouping is always one the screen offers', () => {
    checkMachine({
      machine: groupingMachine,
      input: undefined,
      event: random => ({type: 'group', by: random.pick<GroupBy>(GROUP_BYS)}),
      invariants: snapshot =>
        GROUP_BYS.includes(snapshot.context.by) ? [] : ['an unknown grouping'],
    });
  });
});
