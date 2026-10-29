import {expect, test} from '@playwright/test';
import {checkControls, controlsOf} from './checks/controls-act.js';
import {settled} from './checks/settle.js';
import {main, NEW_RUN_PAGES, openAt} from './new-run-flow.js';

/**
 * Every control of every New run page acts: each control found by role is used once on a fresh
 * copy of the page (the copies side by side in one browser) and must change what the page shows
 * (`checks/controls-act.ts`). A page is opened by the draft that would have brought it there.
 */

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

for (const each of NEW_RUN_PAGES) {
  test(`every control of the ${each.name} page acts`, async ({
    context,
    browser,
    baseURL,
    viewport,
    colorScheme,
    contextOptions,
  }) => {
    const first = await context.newPage();
    await first.setViewportSize({width: 1440, height: 900});
    await openAt(first, each);
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
        await page.setViewportSize({width: 1440, height: 900});
        await openAt(page, each);
      },
      main,
    );
    const inert = found.filter((problem): problem is string => problem !== null);
    expect(inert, `controls that do nothing:\n${inert.join('\n')}`).toEqual([]);
  });
}
