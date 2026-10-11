import {describe, expect, test} from 'bun:test';
import {type FocusReturnTarget, restoreLogicalFocus} from './focus-return.js';

class FakeFocusTarget implements FocusReturnTarget {
  focused = 0;

  constructor(readonly isConnected: boolean) {}

  focus(): void {
    this.focused += 1;
  }
}

describe('sheet focus restoration', () => {
  test('returns to the opener while its rendered row still exists', () => {
    const opener = new FakeFocusTarget(true);
    const fallback = new FakeFocusTarget(true);
    restoreLogicalFocus(opener, fallback);
    expect(opener.focused).toBe(1);
    expect(fallback.focused).toBe(0);
  });

  test('returns to the logical region after a refresh disconnects the opener', () => {
    const opener = new FakeFocusTarget(false);
    const fallback = new FakeFocusTarget(true);
    restoreLogicalFocus(opener, fallback);
    expect(opener.focused).toBe(0);
    expect(fallback.focused).toBe(1);
  });
});
