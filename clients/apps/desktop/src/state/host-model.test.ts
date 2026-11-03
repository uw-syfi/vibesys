import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  ItemSchema,
  ItemStatus,
  WatchAgentResponseSchema,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {generateScenario} from '../testing/orchestrator/index.js';
import {applyHostFrame, emptyHostModel, type HostFrame, runsCursor} from './host-model.js';
import {applyEvent} from './orchestrator/fold.js';
import {emptyState} from './orchestrator/state.js';

const scenarios = fc.record({
  seed: fc.integer({min: 1, max: 500}),
  subagents: fc.integer({min: 0, max: 5}),
  live: fc.boolean(),
});

const eventFrame = (event: ReturnType<typeof generateScenario>['events'][number]): HostFrame => ({
  from: 'runs',
  body: create(WatchRunsResponseSchema, {body: {case: 'event', value: event}}).body,
});

describe('the host model', () => {
  it('folds run events as the fold does, and the run stream never moves an agent cursor', () => {
    fc.assert(
      fc.property(scenarios, options => {
        const {events} = generateScenario(options);
        const model = events.map(eventFrame).reduce(applyHostFrame, emptyHostModel);
        expect(model.data).toEqual(events.reduce(applyEvent, emptyState));
        expect(model.agentCursors).toEqual({});
        expect(runsCursor(model)).toBe(BigInt(events.length));
      }),
      {numRuns: 40},
    );
  });

  it('keeps the newest sequence seen from each agent stream, whatever the arrival order', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            agentId: fc.constantFrom('a1', 'a2'),
            sequence: fc.bigInt({min: 0n, max: 50n}),
          }),
          {maxLength: 20},
        ),
        arrivals => {
          const model = arrivals
            .map(
              ({agentId, sequence}): HostFrame => ({
                from: 'agent',
                agentId,
                frame: create(WatchAgentResponseSchema, {
                  sequence,
                  body: {
                    case: 'item',
                    value: create(ItemSchema, {
                      id: `${agentId}-i`,
                      agentId,
                      status: ItemStatus.RUNNING,
                    }),
                  },
                }),
              }),
            )
            .reduce(applyHostFrame, emptyHostModel);
          for (const agentId of ['a1', 'a2']) {
            const seen = arrivals.filter(a => a.agentId === agentId).map(a => a.sequence);
            expect(model.agentCursors[agentId]).toBe(
              seen.length === 0 ? undefined : seen.reduce((a, b) => (a > b ? a : b)),
            );
          }
        },
      ),
    );
  });

  it('restarts a run stream from a snapshot while a gap stands, and from the sequence otherwise', () => {
    const {events} = generateScenario({seed: 3, subagents: 2, live: false});
    const skipped = events.slice(2, 3).map(eventFrame).reduce(applyHostFrame, emptyHostModel);
    expect(skipped.data.gap).not.toBeNull();
    expect(runsCursor(skipped)).toBe(0n);
    const first = events.slice(0, 1).map(eventFrame).reduce(applyHostFrame, emptyHostModel);
    expect(runsCursor(first)).toBe(1n);
  });
});
