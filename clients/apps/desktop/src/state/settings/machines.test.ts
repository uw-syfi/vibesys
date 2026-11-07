import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {type AddMachineEvent, addMachineStep, emptyAddMachine} from './machines.js';

const event: fc.Arbitrary<AddMachineEvent> = fc.oneof(
  fc.string({maxLength: 6}).map(input => ({type: 'typed', input}) as const),
  fc
    .tuple(fc.string({maxLength: 6}), fc.array(fc.string({maxLength: 4}), {maxLength: 3}))
    .map(([query, aliases]) => ({type: 'matched', query, aliases}) as const),
  fc.constant({type: 'submitted'} as const),
  fc.string({minLength: 1, maxLength: 6}).map(reason => ({type: 'refused', reason}) as const),
);

describe('the add machine input', () => {
  it('offers only the answer for what is typed now', () => {
    fc.assert(
      fc.property(fc.array(event, {maxLength: 12}), events => {
        let state = emptyAddMachine;
        let offered: readonly string[] = [];
        for (const next of events) {
          const before = state;
          state = addMachineStep(state, next);
          if (next.type === 'matched' && next.query === before.input) offered = next.aliases;
          if (next.type === 'typed' && !before.busy) offered = state.matches;
        }
        expect(state.matches).toEqual(offered);
      }),
    );
  });

  it('holds the input while busy and shows a refusal until the next edit', () => {
    let state = addMachineStep(emptyAddMachine, {type: 'typed', input: 'ssh -X h'});
    state = addMachineStep(state, {type: 'submitted'});
    expect(addMachineStep(state, {type: 'typed', input: 'x'}).input).toBe('ssh -X h');
    state = addMachineStep(state, {type: 'refused', reason: '-X is not accepted'});
    expect(state).toMatchObject({busy: false, error: '-X is not accepted'});
    expect(addMachineStep(state, {type: 'typed', input: 'ssh h'}).error).toBeNull();
  });
});
