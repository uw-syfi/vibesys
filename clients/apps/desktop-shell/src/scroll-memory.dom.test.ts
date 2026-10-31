import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {Screen} from '@vibesys/feature-app-shell/state';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {windowAddress} from './address.js';
import {entryOf, ScrollMemory} from './scroll-memory.js';

const page = new Window({url: 'https://app.test/shell.html'});
const installed = Object.getOwnPropertyNames(page).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(page, name),
    });
});
afterAll(async () => {
  for (const name of installed) Reflect.deleteProperty(globalThis, name);
  await page.happyDOM.close();
});

const browser = page as unknown as globalThis.Window;

const SCREENS: readonly Screen[] = [
  {kind: 'runs'},
  {kind: 'machines'},
  {kind: 'usage'},
  {kind: 'settings'},
];

/** Draws `screen` as the app does: a new element that scrolls, as React mounts a screen again. */
function draw(screen: Screen): HTMLElement {
  const body = document.createElement('div');
  body.className = `screen-body-${screen.kind}`;
  document.body.replaceChildren(body);
  return body;
}

async function settled(): Promise<void> {
  await page.happyDOM.waitUntilComplete();
}

type Move =
  | {readonly kind: 'go'; readonly screen: Screen}
  | {readonly kind: 'scroll'; readonly top: number}
  | {readonly kind: 'back'}
  | {readonly kind: 'forward'};

const move: fc.Arbitrary<Move> = fc.oneof(
  {weight: 3, arbitrary: fc.constantFrom(...SCREENS).map((screen): Move => ({kind: 'go', screen}))},
  {weight: 3, arbitrary: fc.integer({min: 1, max: 900}).map((top): Move => ({kind: 'scroll', top}))},
  {weight: 2, arbitrary: fc.constantFrom<Move>({kind: 'back'}, {kind: 'forward'})},
);

test('property: Back and Forward return the screen shown to where it was left, and a new entry starts at the top', async () => {
  await fc.assert(
    fc.asyncProperty(fc.array(move, {maxLength: 25}), async moves => {
      const address = windowAddress(browser, null);
      // The entries this walk made, each with the top it was left at; `at` is the one shown.
      const entries: {screen: Screen; top: number}[] = [{screen: address.screen(), top: 0}];
      let at = 0;
      let body = draw(entries[0]?.screen ?? SCREENS[0] ?? {kind: 'runs'});
      const stop = address.onRestore(screen => {
        body = draw(screen);
      });
      for (const each of moves) {
        if (each.kind === 'scroll') {
          body.scrollTop = each.top;
          const shown = entries[at];
          if (shown !== undefined) shown.top = each.top;
        } else if (each.kind === 'go') {
          const shown = entries[at];
          if (shown?.screen.kind === each.screen.kind) continue;
          address.push(each.screen);
          entries.splice(at + 1, entries.length, {screen: each.screen, top: 0});
          at += 1;
          body = draw(each.screen);
        } else {
          const to = at + (each.kind === 'back' ? -1 : 1);
          if (entries[to] === undefined) continue;
          if (each.kind === 'back') address.back();
          else browser.history.forward();
          at = to;
          await settled();
        }
        expect(body.scrollTop).toBe(entries[at]?.top ?? -1);
      }
      stop();
    }),
    {numRuns: 60, seed: 3205},
  );
});

test('property: a window’s first entry and any state that is not ours is entry 0', () => {
  fc.assert(
    fc.property(fc.anything(), state => {
      const entry = entryOf(state);
      expect(Number.isInteger(entry) && entry >= 0).toBe(true);
    }),
    {numRuns: 200, seed: 3206},
  );
  expect(entryOf({entry: 7})).toBe(7);
  expect(entryOf({entry: -1})).toBe(0);
  expect(entryOf(null)).toBe(0);
});

test('property: what was left on an entry is what is recalled, until a new entry ends it', () => {
  fc.assert(
    fc.property(
      fc.array(fc.tuple(fc.nat(8), fc.dictionary(fc.string(), fc.nat(500))), {maxLength: 20}),
      fc.nat(8),
      (leaves, after) => {
        const memory = new ScrollMemory();
        for (const [entry, scrolls] of leaves) memory.leave(entry, scrolls);
        const last = new Map(leaves);
        memory.forgetAfter(after);
        for (let entry = 0; entry <= 8; entry += 1)
          expect(memory.recall(entry)).toEqual(entry > after ? {} : (last.get(entry) ?? {}));
      },
    ),
    {numRuns: 100, seed: 3207},
  );
});
