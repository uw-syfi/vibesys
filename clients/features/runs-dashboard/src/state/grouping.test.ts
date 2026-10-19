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

  test('shows every machine until one is chosen, and removing it shows every machine again', () => {
    const start = getInitialSnapshot(groupingMachine);
    expect(start.context.machine).toBeNull();
    const narrowed = getNextSnapshot(groupingMachine, start, {
      type: 'filter-machine',
      machine: 'host-a',
    });
    expect(narrowed.context.machine).toBe('host-a');
    const cleared = getNextSnapshot(groupingMachine, narrowed, {type: 'clear-machine'});
    expect(cleared.context.machine).toBeNull();
  });

  test('model: whatever is chosen, the grouping is one the screen offers and the filter one of the machines chosen', () => {
    checkMachine({
      machine: groupingMachine,
      input: undefined,
      event: random => {
        const pick = random.int(3);
        if (pick === 0) return {type: 'group', by: random.pick<GroupBy>(GROUP_BYS)};
        if (pick === 1) return {type: 'clear-machine'};
        return {type: 'filter-machine', machine: random.pick(['host-a', 'host-b'])};
      },
      invariants: snapshot => {
        const {by, machine} = snapshot.context;
        const problems: string[] = [];
        if (!GROUP_BYS.includes(by)) problems.push('an unknown grouping');
        if (machine !== null && machine !== 'host-a' && machine !== 'host-b') {
          problems.push('a machine nobody chose');
        }
        return problems;
      },
    });
  });
});
