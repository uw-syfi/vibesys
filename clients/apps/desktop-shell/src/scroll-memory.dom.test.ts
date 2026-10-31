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
  {
    weight: 3,
    arbitrary: fc.integer({min: 1, max: 900}).map((top): Move => ({kind: 'scroll', top})),
  },
  {weight: 2, arbitrary: fc.constantFrom<Move>({kind: 'back'}, {kind: 'forward'})},
);

/** A walk through the window's history, with the top each entry was left at as the model. */
class Walk {
  readonly address = windowAddress(browser, null);
  readonly entries: {screen: Screen; top: number}[] = [{screen: this.address.screen(), top: 0}];
  at = 0;
  body = draw(this.address.screen());
  readonly stop = this.address.onRestore(screen => {
    this.body = draw(screen);
  });

  get top(): number {
    return this.entries[this.at]?.top ?? -1;
  }

  async apply(each: Move): Promise<void> {
    if (each.kind === 'scroll') this.scroll(each.top);
    else if (each.kind === 'go') this.go(each.screen);
    else await this.step(each.kind === 'back' ? -1 : 1);
  }

  private scroll(top: number): void {
    this.body.scrollTop = top;
    const shown = this.entries[this.at];
    if (shown !== undefined) shown.top = top;
  }

  private go(screen: Screen): void {
    if (this.entries[this.at]?.screen.kind === screen.kind) return;
    this.address.push(screen);
    this.entries.splice(this.at + 1, this.entries.length, {screen, top: 0});
    this.at += 1;
    this.body = draw(screen);
  }

  private async step(by: -1 | 1): Promise<void> {
    if (this.entries[this.at + by] === undefined) return;
    if (by === -1) this.address.back();
    else browser.history.forward();
    this.at += by;
    await settled();
  }
}

test('property: Back and Forward return the screen shown to where it was left, and a new entry starts at the top', async () => {
  await fc.assert(
    fc.asyncProperty(fc.array(move, {maxLength: 25}), async moves => {
      const walk = new Walk();
      for (const each of moves) {
        await walk.apply(each);
        expect(walk.body.scrollTop).toBe(walk.top);
      }
      walk.stop();
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
