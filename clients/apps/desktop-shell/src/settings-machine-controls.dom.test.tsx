/**
 * Every control of every machine Settings page acts: each control found by role is used once on a
 * fresh copy of the page and must change what the page shows (`testing/dom-controls.ts`). A page
 * is reached by the steps a user takes (`testing/settings-pages.ts`, which the browser journeys
 * read too), over the world the browser journeys use. The same audit ran in Chromium, one browser
 * context per control; here a page is drawn by the shell's own stores and screens.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {controlsOf, describeControl, readScreen, use} from './testing/dom-controls.js';
import {reach} from './testing/dom-steps.js';
import {SEED} from './testing/seed.js';
import {SETTINGS_MACHINES, SETTINGS_PAGES} from './testing/settings-pages.js';
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
    page: /^clusters/,
    name: /^Clusters and clouds$/,
    why: 'the navigation entry of the section that is already shown',
  },
  {
    page: /^logins/,
    name: /^Agent logins$/,
    why: 'the navigation entry of the section that is already shown',
  },
  {
    page: /^logins started$/,
    name: /^Sign in$/,
    why: 'the login already started; starting it again shows the same instructions, as they already are',
  },
  {
    page: /tested$|failed$/,
    name: /^Test connection$/,
    why: 'the form has just been tested; the same cluster gives the same answer, shown as it already is',
  },
];

const main = (container: HTMLElement): Element => {
  const found = container.querySelector('main');
  if (found === null) throw new Error('the page has no main landmark');
  return found;
};

for (const each of SETTINGS_PAGES) {
  test(`every control of the ${each.name} page acts`, async () => {
    const open = async () => {
      const shell = await openShell(
        {kind: 'settings'},
        {schedule: new FakeClock().schedule, seed: {...SEED, machines: SETTINGS_MACHINES}},
      );
      await reach(shell, each.steps);
      await shell.read();
      return shell;
    };
    const exempt = INERT_BY_DESIGN.filter(rule => rule.page.test(each.name));
    const first = await open();
    const controls = controlsOf(main(first.container)).filter(
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
