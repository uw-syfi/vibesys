import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {Screen} from '@vibesys/feature-app-shell/state';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {followMouseButtons, windowAddress} from './address.js';

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

/** The page as the DOM's `Window` type has it: the shell's code never sees happy-dom's. */
const browser = page as unknown as globalThis.Window;

const RUN = {host: 'local', runId: 'r1'} as const;
const OTHER: Screen = {kind: 'machines'};

/** What `button`'s release does on `target`: whether it stayed the page's to act on. */
function release(target: EventTarget, button: number): boolean {
  return target.dispatchEvent(new MouseEvent('mouseup', {button, bubbles: true, cancelable: true}));
}

/** Lets the window finish going Back or Forward. */
async function settled(): Promise<void> {
  await page.happyDOM.waitUntilComplete();
}

/** A page with `screens` pushed. */
function walked(...screens: readonly Screen[]) {
  const address = windowAddress(browser, null);
  for (const screen of screens) address.push(screen);
  return address;
}

test('the address names the location, and Back restores the entry before it', async () => {
  const address = walked(OTHER, {kind: 'run', run: RUN, zoom: 'idea=i1'});
  expect(page.location.hash).toBe('#run=local/r1&idea=i1');
  expect(address.screen()).toEqual({kind: 'run', run: RUN, zoom: 'idea=i1'});
  const restored: Screen[] = [];
  const stop = address.onRestore(screen => restored.push(screen));
  address.back();
  await settled();
  stop();
  expect(restored).toEqual([OTHER]);
});

test('a run window’s query names its run: its fragment is the zoom alone', () => {
  const address = windowAddress(browser, RUN);
  address.push({kind: 'run', run: RUN, zoom: 'mode=logs'});
  expect(page.location.hash).toBe('#mode=logs');
  expect(address.screen()).toEqual({kind: 'run', run: RUN, zoom: 'mode=logs'});
  address.push({kind: 'run', run: RUN});
  expect(page.location.hash).toBe('');
  expect(address.screen()).toEqual({kind: 'run', run: RUN});
});

test('the back and forward buttons go Back and Forward, once per press, over any element', async () => {
  const stop = followMouseButtons(browser);
  const address = windowAddress(browser, null);
  address.push({kind: 'usage'});
  address.push(OTHER);
  const field = document.createElement('textarea');
  field.value = 'typed';
  document.body.append(field);
  expect(release(field, 3)).toBe(false);
  await settled();
  expect(page.location.hash).toBe('#usage');
  expect(release(document.body, 4)).toBe(false);
  await settled();
  expect(page.location.hash).toBe('#machines');
  expect(field.value).toBe('typed');
  field.remove();
  stop();
  expect(release(document.body, 3)).toBe(true);
  await settled();
  expect(page.location.hash).toBe('#machines');
});

test('property: only the back and forward buttons move the window, and a handled press does not', async () => {
  const stop = followMouseButtons(browser);
  const address = windowAddress(browser, null);
  address.push({kind: 'usage'});
  address.push(OTHER);
  await fc.assert(
    fc.asyncProperty(fc.constantFrom(0, 1, 2, 5, 6), async button => {
      expect(release(document.body, button)).toBe(true);
      await settled();
      expect(page.location.hash).toBe('#machines');
    }),
    {numRuns: 5, seed: 3204},
  );
  const handled = new MouseEvent('mouseup', {button: 3, bubbles: true, cancelable: true});
  handled.preventDefault();
  document.body.dispatchEvent(handled);
  await settled();
  expect(page.location.hash).toBe('#machines');
  stop();
});
