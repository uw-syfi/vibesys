import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {LifecycleSchema, Outcome, RunLiveness} from '@vibesys/proto';
import * as intents from './index.js';

test('every ended observation offers Resume while a live writer never does', () => {
  for (let epoch = 1; epoch <= 5; epoch += 1) {
    const observed = BigInt(epoch);
    for (const [state, lifecycle] of [
      ['parked', create(LifecycleSchema, {state: {case: 'parked', value: {}}})],
      [
        'ended',
        create(LifecycleSchema, {
          state: {case: 'ended', value: {outcome: Outcome.STOPPED, resumable: true}},
        }),
      ],
      [
        'ended',
        create(LifecycleSchema, {
          state: {case: 'ended', value: {outcome: Outcome.INTERRUPTED, resumable: true}},
        }),
      ],
    ] as const) {
      expect(
        intents.relaunchTarget(
          {epoch: observed, lifecycle},
          {epoch: observed, value: RunLiveness.ENDED},
        ),
      ).toEqual({epoch: observed, state});
      expect(
        intents.relaunchTarget(
          {epoch: observed, lifecycle},
          {epoch: observed, value: RunLiveness.ALIVE},
        ),
      ).toBeNull();
    }
    // The host admits Resume after every ending; the flag it keeps for older readers is ignored.
    for (const outcome of [Outcome.FINISHED, Outcome.FAILED])
      for (const resumable of [true, false]) {
        const lifecycle = create(LifecycleSchema, {
          state: {case: 'ended', value: {outcome, resumable}},
        });
        for (const value of [RunLiveness.ENDED, RunLiveness.INTERRUPTED, RunLiveness.UNSPECIFIED])
          expect(
            intents.relaunchTarget({epoch: observed, lifecycle}, {epoch: observed, value}),
          ).toEqual({epoch: observed, state: 'ended'});
        expect(
          intents.relaunchTarget(
            {epoch: observed, lifecycle},
            {epoch: observed, value: RunLiveness.ALIVE},
          ),
        ).toBeNull();
      }
    const lifecycle = create(LifecycleSchema, {state: {case: 'running', value: {}}});
    expect(
      intents.relaunchTarget(
        {epoch: observed, lifecycle},
        {epoch: observed, value: RunLiveness.INTERRUPTED},
      ),
    ).toEqual({epoch: observed, state: 'interrupted'});
    expect(
      intents.relaunchTarget(
        {epoch: observed, lifecycle},
        {epoch: observed - 1n, value: RunLiveness.INTERRUPTED},
      ),
    ).toBeNull();
    expect(
      intents.relaunchTarget(
        {epoch: observed, lifecycle},
        {epoch: observed, value: RunLiveness.UNSPECIFIED},
      ),
    ).toBeNull();
  }
});

test('a newer host epoch fences an older parked snapshot', () => {
  const lifecycle = create(LifecycleSchema, {state: {case: 'parked', value: {}}});
  for (const value of [RunLiveness.ALIVE, RunLiveness.INTERRUPTED, RunLiveness.ENDED]) {
    expect(intents.relaunchTarget({epoch: 1n, lifecycle}, {epoch: 2n, value})).toBeNull();
  }
});
