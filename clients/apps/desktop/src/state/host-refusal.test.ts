import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {StateRefusal, UnservedReason, UnservedSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {REFUSAL_CAUSES} from './fault.js';
import {refusalOf} from './host-refusal.js';

const reasons = [
  UnservedReason.HELD_ELSEWHERE,
  UnservedReason.STATE_REFUSED,
  UnservedReason.CONFIG_REFUSED,
  UnservedReason.HOLDER_GONE,
] as const;
const places = [
  StateRefusal.UNSPECIFIED,
  StateRefusal.NETWORK_FILESYSTEM,
  StateRefusal.UNKNOWN_FILESYSTEM,
] as const;

const unserved = (reason: UnservedReason, stateRefusal: StateRefusal, detail: string) =>
  create(UnservedSchema, {reason, stateRefusal, detail});

describe('what a host that does not serve refuses', () => {
  it('is a refusal for the reasons no waiting fixes, and its own words travel unchanged', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...reasons),
        fc.constantFrom(...places),
        fc.string(),
        (reason, place, detail) => {
          const refusal = refusalOf(unserved(reason, place, detail));
          const forGood =
            reason === UnservedReason.STATE_REFUSED || reason === UnservedReason.CONFIG_REFUSED;
          // A host held by another process, or whose holder has gone, is waited out, not refused.
          expect(refusal === undefined).toBe(!forGood);
          if (refusal === undefined) return;
          expect(REFUSAL_CAUSES).toContain(refusal.cause);
          expect(refusal.detail).toBe(detail);
        },
      ),
    );
  });

  it('names a network disk and a disk not known to be local apart from every other refusal', () => {
    const cause = (reason: UnservedReason, place: StateRefusal) =>
      refusalOf(unserved(reason, place, ''))?.cause;
    const refused = UnservedReason.STATE_REFUSED;
    expect(cause(refused, StateRefusal.NETWORK_FILESYSTEM)).toBe('state-network-filesystem');
    expect(cause(refused, StateRefusal.UNKNOWN_FILESYSTEM)).toBe('state-unknown-filesystem');
    expect(cause(refused, StateRefusal.UNSPECIFIED)).toBe('state-refused');
    // The place of a state directory says nothing about an unreadable configuration.
    expect(cause(UnservedReason.CONFIG_REFUSED, StateRefusal.NETWORK_FILESYSTEM)).toBe(
      'config-refused',
    );
  });
});
