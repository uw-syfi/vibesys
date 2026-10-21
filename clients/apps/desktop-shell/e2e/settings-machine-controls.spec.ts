import {expect, test} from '@playwright/test';
import {checkControls, controlsOf} from './checks/controls-act.js';
import {settled} from './checks/settle.js';
import {main, openSettingsAt} from './settings-machine.js';
import {SETTINGS_PAGES} from './settings-machine-pages.js';

/**
 * Every control of every machine Settings page acts: each control found by role is used once on a
 * fresh copy of the page (the copies side by side in one browser) and must change what the page
 * shows (`checks/controls-act.ts`).
 */

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

for (const each of SETTINGS_PAGES) {
  test(`every control of the ${each.name} page acts`, async ({
    context,
    browser,
    baseURL,
    viewport,
    colorScheme,
    contextOptions,
  }) => {
    const first = await context.newPage();
    await openSettingsAt(first, 1440);
    await each.reach(first);
    await settled(first);
    const exempt = INERT_BY_DESIGN.filter(rule => rule.page.test(each.name));
    const controls = (await controlsOf(main(first))).filter(
      control => !exempt.some(rule => rule.name.test(control.name)),
    );
    await first.close();
    expect(controls.length, 'the page has controls').toBeGreaterThan(0);
    const found = await checkControls(
      browser,
      {...contextOptions, ...(baseURL === undefined ? {} : {baseURL}), viewport, colorScheme},
      controls,
      async page => {
        await openSettingsAt(page, 1440);
        await each.reach(page);
      },
      main,
    );
    const inert = found.filter((problem): problem is string => problem !== null);
    expect(inert, `controls that do nothing:\n${inert.join('\n')}`).toEqual([]);
  });
}
