import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {isBack, type KeyPress} from './keys.js';

const press = fc.record({
  key: fc.constantFrom('Escape', 'ArrowLeft', 'ArrowRight', 'Enter', 'a'),
  alt: fc.boolean(),
  ctrl: fc.boolean(),
  meta: fc.boolean(),
  shift: fc.boolean(),
  handled: fc.boolean(),
  capturing: fc.boolean(),
});

const plain: KeyPress = {
  key: 'Escape',
  alt: false,
  ctrl: false,
  meta: false,
  shift: false,
  handled: false,
  capturing: false,
};

describe('the back keys', () => {
  it('are Escape alone and Alt+Left alone', () => {
    expect(isBack(plain)).toBe(true);
    expect(isBack({...plain, key: 'ArrowLeft', alt: true})).toBe(true);
    expect(isBack({...plain, key: 'ArrowLeft'})).toBe(false);
    expect(isBack({...plain, key: 'ArrowLeft', alt: true, shift: true})).toBe(false);
    expect(isBack({...plain, shift: true})).toBe(false);
  });

  it('never fire where another handler or a control already uses the key', () => {
    fc.assert(
      fc.property(press, candidate => {
        if (candidate.handled || candidate.capturing) expect(isBack(candidate)).toBe(false);
      }),
    );
  });

  it('fire for no key but those two', () => {
    fc.assert(
      fc.property(press, candidate => {
        if (isBack(candidate)) {
          expect(['Escape', 'ArrowLeft']).toContain(candidate.key);
          expect(candidate.key === 'ArrowLeft' ? candidate.alt : !candidate.alt).toBe(true);
        }
      }),
    );
  });
});
