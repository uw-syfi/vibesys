/**
 * Every control of every New run page acts: each control found by role is used once on a fresh
 * copy of the page and must change what the page shows (`testing/dom-controls.ts`). A page is
 * opened by the draft that would have brought it there, over the world the browser journeys use.
 * The same audit ran in Chromium, one browser context per control (about 0.8 s each); here a page
 * is drawn by the shell's own stores and screens in about 25 ms.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {draftAt, NEW_RUN_PAGES} from '../e2e/new-run-pages.js';
import {
  type Control,
  controlsOf,
  describeControl,
  readScreen,
  use,
} from './testing/dom-controls.js';
import {openShell} from './testing/shell-world.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

/** Controls that change nothing on their page by design, each with why. */
const INERT_BY_DESIGN: readonly {
  readonly page: RegExp;
  readonly name: RegExp;
  readonly why: string;
}[] = [
  {
    page: /^project$/,
    name: /^Folder on /,
    why: 'a path field that offers completions as it is typed in; "the folder field completes paths" drives it',
  },
  {
    page: /^where/,
    name: /^Same machine/,
    why: 'the place measured on until another is chosen; it is already the chosen one',
  },
];

const main = (container: HTMLElement): Element => {
  const found = container.querySelector('main');
  if (found === null) throw new Error('the page has no main landmark');
  return found;
};

for (const each of NEW_RUN_PAGES) {
  test(`every control of the ${each.name} page acts`, async () => {
    const exempt = INERT_BY_DESIGN.filter(rule => rule.page.test(each.name));
    const open = () => openShell({kind: 'new-run'}, {draft: draftAt(each)});
    const first = await open();
    const controls: Control[] = controlsOf(main(first.container)).filter(
      control => !exempt.some(rule => rule.name.test(control.name)),
    );
    await first.close();
    expect(controls.length, 'the page has controls').toBeGreaterThan(0);
    const inert: string[] = [];
    for (const control of controls) {
      const shell = await open();
      try {
        const before = readScreen(shell.container);
        await use(main(shell.container), control);
        await shell.read();
        if (readScreen(shell.container) === before) {
          inert.push(`${describeControl(control)} changes nothing on the screen`);
        }
      } finally {
        await shell.close();
      }
    }
    expect(inert, `controls that do nothing:\n${inert.join('\n')}`).toEqual([]);
  });
}
