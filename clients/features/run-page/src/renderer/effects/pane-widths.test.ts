import {expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {PANE_ROLES, type PaneWidths} from '../../state/index.js';
import {browserPaneWidths, PANE_WIDTHS_KEY} from './pane-widths.js';

/** A Storage over a map: faithful for the two calls the memory makes. */
function fakeStorage(initial: Record<string, string> = {}): Storage {
  const data = new Map(Object.entries(initial));
  return {
    getItem: key => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
  } as Storage;
}

test('the widths written are read back, for any roles and widths', () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const storage = fakeStorage();
    const widths: PaneWidths = Object.fromEntries(
      PANE_ROLES.filter(() => random.bool()).map(role => [role, 1 + random.int(2000)]),
    );
    browserPaneWidths(() => storage).write(widths);
    expect(browserPaneWidths(() => storage).read()).toEqual(widths);
  }
});

test('a stored value that is not a width is no width, so its pane keeps its default', () => {
  for (const text of ['', 'not json', '[400]', 'null', '3', '"wide"']) {
    expect(browserPaneWidths(() => fakeStorage({[PANE_WIDTHS_KEY]: text})).read()).toEqual({});
  }
  const mixed = JSON.stringify({
    master: 420,
    now: '380',
    agents: 0,
    wide: -5,
    chat: Number.MAX_VALUE * 2,
    sidebar: 300,
  });
  expect(browserPaneWidths(() => fakeStorage({[PANE_WIDTHS_KEY]: mixed})).read()).toEqual({
    master: 420,
  });
});

test('a missing or failing storage keeps nothing and throws nothing', () => {
  const none = browserPaneWidths(() => null);
  none.write({master: 500});
  expect(none.read()).toEqual({});
  const failing = browserPaneWidths(() => {
    throw new Error('blocked');
  });
  failing.write({master: 500});
  expect(failing.read()).toEqual({});
  const full = browserPaneWidths(
    () =>
      ({
        getItem: () => null,
        setItem: () => {
          throw new Error('quota');
        },
      }) as unknown as Storage,
  );
  full.write({master: 500});
  expect(full.read()).toEqual({});
});
