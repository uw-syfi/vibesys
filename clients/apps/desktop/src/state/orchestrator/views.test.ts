import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {ItemSchema, ItemStatus} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {emptyState, type OrchestratorState} from './state.js';
import {unbrokenFrom} from './views.js';

describe('reading older steps', () => {
  const stateOf = (ordinals: readonly number[]): OrchestratorState => ({
    ...emptyState,
    items: Object.fromEntries(
      ordinals.map(ordinal => [
        `item-${ordinal}`,
        create(ItemSchema, {
          id: `item-${ordinal}`,
          agentId: 'a',
          ordinal,
          status: ItemStatus.FINISHED,
        }),
      ]),
    ),
  });

  it('paging below the unbroken run fills every hole and reaches the first step', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 80}),
        fc.array(fc.integer({min: 1, max: 80}), {minLength: 1, maxLength: 30}),
        fc.integer({min: 1, max: 12}),
        (total, held, pageSize) => {
          const have = new Set(held.filter(ordinal => ordinal <= total));
          if (have.size === 0) have.add(total);
          let state = stateOf([...have]);
          // The host answers "items below this ordinal, newest first".
          for (let reads = 0; reads <= total; reads++) {
            const before = unbrokenFrom(state, 'a');
            if (before === null || before <= 1) break;
            const page = Array.from(
              {length: Math.min(pageSize, before - 1)},
              (_, i) => before - 1 - i,
            );
            state = stateOf([...Object.values(state.items).map(item => item.ordinal), ...page]);
          }
          expect(unbrokenFrom(state, 'a')).toBe(1);
        },
      ),
    );
  });
});
