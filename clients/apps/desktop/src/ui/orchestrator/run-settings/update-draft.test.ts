import {describe, expect, it} from '@effect/vitest';
import {Harness} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {RunSettings} from '../../../state/index.js';
import {updateRunMessage} from '../intents.js';
import {LEADS} from '../start-draft.js';
import {asked, draftOf, NO_LIMIT, type SettingsDraft} from './update-draft.js';

const lead = fc.record({
  harness: fc.constantFrom(Harness.CLAUDE_CODE, Harness.CODEX),
  model: fc.constantFrom('', 'model-a', 'model-b'),
  effort: fc.constant(''),
});

const settings: fc.Arbitrary<RunSettings> = fc.record({
  cost: fc.record({
    limitUsd: fc.option(fc.integer({min: 1, max: 900}), {nil: null}),
    spentUsd: fc.option(fc.integer({min: 0, max: 900}), {nil: null}),
    setupUsd: fc.option(fc.integer({min: 0, max: 20}), {nil: null}),
  }),
  time: fc.record({
    limitSeconds: fc.option(fc.integer({min: 60, max: 200_000}), {nil: null}),
    elapsedSeconds: fc.option(fc.integer({min: 0, max: 200_000}), {nil: null}),
    setupSeconds: fc.option(fc.integer({min: 0, max: 5000}), {nil: null}),
  }),
  fanout: fc.record({
    cap: fc.integer({min: 1, max: 8}),
    live: fc.option(fc.integer({min: 0, max: 8}), {nil: null}),
    above: fc.constant(0),
  }),
  lead: fc.record({
    current: fc.option(lead, {nil: null}),
    pending: fc.option(lead, {nil: null}),
  }),
});

const request = (draft: SettingsDraft, held: RunSettings) => {
  const outcome = asked(draft, held);
  return outcome.kind === 'update' ? outcome.request : undefined;
};

describe('the settings form', () => {
  it('opens at what the run has, and asks nothing', () => {
    fc.assert(
      fc.property(settings, held => {
        expect(asked(draftOf(held), held)).toEqual({kind: 'nothing'});
      }),
    );
  });

  it('asks exactly the setting that was edited, and no other', () => {
    fc.assert(
      fc.property(settings, fc.integer({min: 1, max: 900}), (held, usd) => {
        const draft = draftOf(held);
        fc.pre(held.cost.limitUsd !== usd);
        expect(request({...draft, cost: String(usd)}, held)).toEqual({cost: {set: usd}});
        fc.pre(held.fanout.cap !== 9);
        expect(request({...draft, fanout: '9'}, held)).toEqual({fanout: 9});
        fc.pre(held.time.limitSeconds !== 90);
        expect(request({...draft, time: '90'}, held)).toEqual({time: {set: 90}});
      }),
    );
  });

  it('clears a limit that is set by emptying it, and asks nothing for one already clear', () => {
    fc.assert(
      fc.property(settings, held => {
        const draft = draftOf(held);
        const cleared = {...draft, cost: '', time: NO_LIMIT};
        const asking = request(cleared, held);
        expect(asking?.cost).toEqual(held.cost.limitUsd === null ? undefined : {remove: true});
        expect(asking?.time).toEqual(held.time.limitSeconds === null ? undefined : {remove: true});
      }),
    );
  });

  it('refuses a cost that is not a positive amount before asking anything', () => {
    fc.assert(
      fc.property(settings, fc.constantFrom('abc', '-3', '0', '1e999x', '$5'), (held, typed) => {
        expect(asked({...draftOf(held), cost: typed}, held)).toEqual({
          kind: 'invalid',
          field: 'cost',
        });
      }),
    );
  });

  it('asks the lead only when its tool or model differs from the waiting or running one', () => {
    fc.assert(
      fc.property(settings, lead, (held, chosen) => {
        fc.pre(held.lead.current !== null || held.lead.pending !== null);
        const draft = draftOf(held);
        const base = held.lead.pending ?? held.lead.current;
        const next = {
          ...draft,
          lead: chosen.harness === Harness.CODEX ? 'codex' : 'claude_code',
        } as const;
        const asking = request({...next, model: chosen.model}, held);
        const same = base?.harness === chosen.harness && base?.model === chosen.model;
        expect(asking?.lead === undefined).toBe(same);
        if (asking?.lead !== undefined)
          expect(asking.lead).toEqual({harness: LEADS[next.lead], model: chosen.model});
      }),
    );
  });

  it('makes a command that names each asked setting and none that was not', () => {
    fc.assert(
      fc.property(settings, fc.integer({min: 1, max: 900}), (held, usd) => {
        fc.pre(held.cost.limitUsd !== usd);
        const asking = request({...draftOf(held), cost: String(usd)}, held);
        const message = updateRunMessage('run-1', asking ?? {});
        expect(message.costLimit?.change).toEqual({case: 'setUsd', value: usd});
        expect(message.timeLimit).toBeUndefined();
        expect(message.fanoutLimit).toBeUndefined();
        expect(message.lead).toBeUndefined();
      }),
    );
  });
});
