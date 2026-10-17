import {expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {browserMoreOptionsMemory, MORE_OPTIONS_KEY} from './more-options-memory.js';

/** A Storage over a map: faithful for the two calls the memory makes. */
function fakeStorage(initial: Record<string, string> = {}): Storage {
  const data = new Map(Object.entries(initial));
  return {
    getItem: key => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
  } as Storage;
}

test('what is written is read back, for any machines and choices', () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const storage = fakeStorage();
    const memory = browserMoreOptionsMemory(() => storage);
    const choices = Object.fromEntries(
      Array.from({length: random.int(5)}, (_, index) => [`ssh:host-${index}`, random.bool()]),
    );
    memory.write(choices);
    expect(browserMoreOptionsMemory(() => storage).read()).toEqual(choices);
  }
});

test('anything unreadable is no choices, and a missing or failing storage keeps nothing', () => {
  for (const text of ['', 'not json', '[1]', 'null', '3', '{"a": 1, "b": true}']) {
    const read = browserMoreOptionsMemory(() => fakeStorage({[MORE_OPTIONS_KEY]: text})).read();
    expect(Object.values(read).every(value => typeof value === 'boolean')).toBe(true);
  }
  expect(
    browserMoreOptionsMemory(() => fakeStorage({[MORE_OPTIONS_KEY]: '{"a": 1, "b": true}'})).read(),
  ).toEqual({b: true});
  const none = browserMoreOptionsMemory(() => null);
  none.write({a: true});
  expect(none.read()).toEqual({});
  const failing = browserMoreOptionsMemory(() => {
    throw new Error('blocked');
  });
  failing.write({a: true});
  expect(failing.read()).toEqual({});
});
