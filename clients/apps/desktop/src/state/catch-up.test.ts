import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {type Command, CommandSchema, ReceiptSchema, Refusal} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {catchUpRefusalOf, pressOf} from './catch-up.js';
import type {CommandProgress} from './commands.js';
import type {Fault, FaultKind} from './fault.js';
import {FAULT_KINDS} from './fault.js';

const command: Command = create(CommandSchema, {
  id: 'c1',
  body: {case: 'requestCatchUp', value: {runId: 'run-1'}},
});

const fault = (kind: FaultKind, detail: string): Fault => ({kind, detail, retryable: false});

/** Every refusal the host has, the way the proto names them. */
const REFUSALS = Object.values(Refusal).filter(
  (value): value is Refusal => typeof value === 'number' && value !== Refusal.UNSPECIFIED,
);

describe('a press of Catch me up, as the screen reads it', () => {
  it('is nothing before any press', () => {
    expect(pressOf(undefined)).toBeUndefined();
  });

  it('is on its way while the command is being sent', () => {
    expect(pressOf({_tag: 'Sending', command})).toEqual({_tag: 'Pressing'});
  });

  it('is the check-in the host made', () => {
    const receipt = create(ReceiptSchema, {commandId: 'c1', checkInId: 'ci-1'});
    expect(pressOf({_tag: 'Accepted', receipt})).toEqual({_tag: 'Made', checkInId: 'ci-1'});
  });

  it('is a failure, never a check-in, when the host accepted it and named none', () => {
    const receipt = create(ReceiptSchema, {commandId: 'c1'});
    expect(pressOf({_tag: 'Accepted', receipt})).toMatchObject({
      _tag: 'Failed',
      fault: {kind: 'InvalidMessage'},
    });
  });

  it('is a typed refusal for each refusal the host can give, and never a check-in', () => {
    fc.assert(
      fc.property(fc.constantFrom(...REFUSALS), refusal => {
        const receipt = create(ReceiptSchema, {commandId: 'c1', refusal});
        const press = pressOf({_tag: 'Refused', receipt});
        expect(press).toEqual({_tag: 'Refused', reason: catchUpRefusalOf(refusal)});
      }),
    );
    expect(catchUpRefusalOf(Refusal.RUN_NOT_FOUND)).toBe('run-not-found');
    expect(catchUpRefusalOf(Refusal.NO_VIEWER)).toBe('no-viewer');
    expect(catchUpRefusalOf(Refusal.NOT_PERMITTED)).toBe('not-permitted');
    expect(catchUpRefusalOf(Refusal.COST_LIMIT_REACHED)).toBe('unexpected');
  });

  it('keeps an outcome it cannot tell as unconfirmed, with the fault, to press again', () => {
    fc.assert(
      fc.property(fc.constantFrom(...FAULT_KINDS), kind => {
        const found = fault(kind, 'no reply');
        expect(pressOf({_tag: 'Unconfirmed', command, fault: found})).toEqual({
          _tag: 'Unconfirmed',
          fault: found,
        });
      }),
    );
  });

  it('reads a call refused for naming no viewer as that refusal, whichever field the host names', () => {
    for (const field of ['vibesys-viewer-id', 'vibesys-viewer-label']) {
      const failed: CommandProgress = {
        _tag: 'Failed',
        command,
        fault: fault('Rejected', `the call's ${field} metadata is missing`),
      };
      expect(pressOf(failed)).toEqual({_tag: 'Refused', reason: 'no-viewer'});
    }
  });

  it('reads any other failure as a failure with its fault', () => {
    fc.assert(
      fc.property(fc.constantFrom(...FAULT_KINDS), fc.string(), (kind, detail) => {
        fc.pre(!detail.includes('vibesys-viewer'));
        const found = fault(kind, detail);
        expect(pressOf({_tag: 'Failed', command, fault: found})).toEqual({
          _tag: 'Failed',
          fault: found,
        });
      }),
    );
  });
});
