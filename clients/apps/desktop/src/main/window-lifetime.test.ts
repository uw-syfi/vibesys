import fc from 'fast-check';
import {expect, it} from 'vitest';
import {trackWindow, type WindowLifetime} from './window-lifetime.js';

/** Electron destroys webContents before delivering the BrowserWindow's closed event. */
class FakeWindow implements WindowLifetime {
  #destroyed = false;
  readonly #closed: Array<() => void> = [];

  constructor(private readonly id: number) {}

  get webContents(): {readonly id: number} {
    if (this.#destroyed) throw new TypeError('Object has been destroyed');
    return {id: this.id};
  }

  on(_event: 'closed', listener: () => void): void {
    this.#closed.push(listener);
  }

  close(): void {
    this.#destroyed = true;
    for (const listener of this.#closed) listener();
  }
}

it('revokes a destroyed window without throwing during quit (#3854)', () => {
  const windows = new Set<number>();
  const window = new FakeWindow(7);
  trackWindow(windows, window);
  expect(windows.has(7)).toBe(true);
  expect(() => window.close()).not.toThrow();
  expect(windows.size).toBe(0);
});

it('revokes exactly the closed windows, in any order', () => {
  const order = fc
    .uniqueArray(fc.nat(), {maxLength: 20})
    .chain(ids =>
      fc.tuple(
        fc.constant(ids),
        fc.shuffledSubarray(ids, {minLength: ids.length, maxLength: ids.length}),
      ),
    );
  fc.assert(
    fc.property(order, ([ids, closing]) => {
      const windows = new Set<number>();
      const live = new Map(ids.map(id => [id, new FakeWindow(id)]));
      for (const window of live.values()) trackWindow(windows, window);
      const expected = new Set(ids);
      expect(windows).toEqual(expected);
      for (const id of closing) {
        live.get(id)?.close();
        expected.delete(id);
        expect(windows).toEqual(expected);
      }
    }),
  );
});
