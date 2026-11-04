import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  type Command,
  CommandSchema,
  type Receipt,
  ReceiptSchema,
  Refusal,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {answered, type CommandProgress, settle} from './commands.js';
import type {Fault} from './fault.js';

const fault: Fault = {kind: 'DeadlineExceeded', detail: 'no reply in 15 s', retryable: true};
const command = (id: string): Command =>
  create(CommandSchema, {id, body: {case: 'stopRun', value: {runId: 'run-1'}}});
const receipt = (id: string, refusal: Refusal): Receipt =>
  create(ReceiptSchema, {commandId: id, refusal});

const refusals = fc.constantFrom(
  Refusal.UNSPECIFIED,
  Refusal.RUN_NOT_FOUND,
  Refusal.NOT_PERMITTED,
  Refusal.DEADLINE_PASSED,
);
const ids = fc.constantFrom('c1', 'c2', 'c3');
const progress: fc.Arbitrary<CommandProgress> = ids.chain(id =>
  fc.constantFrom<CommandProgress>(
    {_tag: 'Sending', command: command(id)},
    {_tag: 'Unconfirmed', command: command(id), fault},
    {_tag: 'Failed', command: command(id), fault},
    {_tag: 'Accepted', receipt: receipt(id, Refusal.UNSPECIFIED)},
    {_tag: 'Refused', receipt: receipt(id, Refusal.RUN_NOT_FOUND)},
  ),
);
const receipts = fc
  .array(fc.tuple(ids, refusals), {maxLength: 4})
  .map(rows => Object.fromEntries(rows.map(([id, refusal]) => [id, receipt(id, refusal)])));

describe('a command waiting for its answer', () => {
  it('becomes Accepted or Refused from the receipt of its own id, by the refusal it carries', () => {
    fc.assert(
      fc.property(ids, refusals, (id, refusal) => {
        const held = {[id]: receipt(id, refusal)};
        const expected = refusal === Refusal.UNSPECIFIED ? 'Accepted' : 'Refused';
        for (const waiting of [
          {_tag: 'Sending', command: command(id)},
          {_tag: 'Unconfirmed', command: command(id), fault},
        ] as const) {
          expect(settle(waiting, held)._tag).toBe(expected);
        }
      }),
    );
  });

  it('is left alone when no receipt for its id has arrived', () => {
    const waiting: CommandProgress = {_tag: 'Unconfirmed', command: command('c1'), fault};
    expect(settle(waiting, {c2: receipt('c2', Refusal.UNSPECIFIED)})).toBe(waiting);
  });

  it('never moves once settled or failed, and settling twice equals settling once', () => {
    fc.assert(
      fc.property(progress, receipts, (p, held) => {
        const once = settle(p, held);
        expect(settle(once, held)).toEqual(once);
        if (p._tag === 'Accepted' || p._tag === 'Refused' || p._tag === 'Failed') {
          expect(once).toBe(p);
        }
      }),
    );
  });

  it('is answered as Accepted exactly when the receipt carries no refusal', () => {
    fc.assert(
      fc.property(refusals, refusal => {
        expect(answered(receipt('c1', refusal))._tag).toBe(
          refusal === Refusal.UNSPECIFIED ? 'Accepted' : 'Refused',
        );
      }),
    );
  });
});
