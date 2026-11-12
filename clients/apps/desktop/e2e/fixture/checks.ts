import {expect, type Page} from '@playwright/test';

/** The faces the desktop type scale draws; each must resolve before a screen is measured. */
const BUNDLED_FACES = [
  '400 14px "Schibsted Grotesk"',
  '500 14px "Schibsted Grotesk"',
  '600 24px "Schibsted Grotesk"',
  '400 12.5px "JetBrains Mono"',
  '500 11.5px "JetBrains Mono"',
  '600 22px "JetBrains Mono"',
] as const;

/** An app-window size measured by the fixture suite. */
export interface FixtureViewport {
  readonly name: string;
  readonly width: number;
  readonly height: number;
}

/** The real minimum, then the narrow, default and wide approval sizes. */
export const CHECK_VIEWPORTS: readonly FixtureViewport[] = [
  {name: 'minimum', width: 480, height: 600},
  {name: 'narrow', width: 640, height: 800},
  {name: 'default', width: 1280, height: 860},
  {name: 'wide', width: 1760, height: 1000},
];

/** The three sizes the desktop visual-review workflow puts on its contact sheet. */
export const APPROVAL_VIEWPORTS: readonly FixtureViewport[] = CHECK_VIEWPORTS.slice(1);

/** Uncaught exceptions and console errors reported after observation began. */
export interface PageIssueLog {
  readonly issues: readonly string[];
  expectNone(context: string): void;
}

/** Observe errors before navigation, so an exception during startup cannot escape the check. */
export function observePageIssues(page: Page): PageIssueLog {
  const issues: string[] = [];
  page.on('pageerror', error => issues.push(`uncaught: ${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error') issues.push(`console: ${message.text()}`);
  });
  return {
    issues,
    expectNone: context => expect(issues, `${context}: errors reported by the page`).toEqual([]),
  };
}

interface FontReadiness {
  readonly status: string;
  readonly missing: readonly string[];
}

/** Wait for the faces the type scale uses, then prove none silently fell back. */
export async function expectBundledFontsReady(page: Page): Promise<void> {
  const faces = JSON.stringify(BUNDLED_FACES);
  const readiness = await page.evaluate<FontReadiness>(`(async () => {
    const faces = ${faces};
    await Promise.all(faces.map(face => document.fonts.load(face)));
    await document.fonts.ready;
    return {
      status: document.fonts.status,
      missing: faces.filter(face => !document.fonts.check(face)),
    };
  })()`);
  expect(readiness.status, 'the fixture font set is ready').toBe('loaded');
  expect(readiness.missing, 'bundled font faces that fell back').toEqual([]);
}

/** Wait for finite UI motion by its own completion, never by elapsed time. */
export async function waitForFiniteAnimations(page: Page): Promise<void> {
  await page.evaluate(`Promise.all(
    document.getAnimations()
      .filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity)
      .map(animation => animation.finished.catch(() => undefined))
  )`);
}

/** What the screen declares about vertical scrolling. */
export interface VerticalScrollReport {
  readonly pageRegions: readonly string[];
  readonly undeclared: readonly string[];
  readonly wrongAxis: readonly string[];
}

/** Read the scroll contract from rendered layout, for both the fixture and synthetic check tests. */
export async function verticalScrollReport(page: Page): Promise<VerticalScrollReport> {
  return page.evaluate<VerticalScrollReport>(`(() => {
    const describe = element => {
      const id = element.id === '' ? '' : '#' + element.id;
      const classes = Array.from(element.classList).map(name => '.' + name).join('');
      return element.tagName.toLowerCase() + id + classes;
    };
    const mains = Array.from(document.querySelectorAll('main'));
    const main = mains[0];
    const pageRegions = main === undefined ? [] : [
      ...(main.matches('[data-scroll="column"]') ? [main] : []),
      ...main.querySelectorAll(':scope > .vs-page-content > [data-scroll="column"]'),
    ].map(describe);
    const undeclared = [];
    const wrongAxis = [];
    for (const element of document.querySelectorAll('*')) {
      const style = getComputedStyle(element);
      if (!['auto', 'scroll', 'overlay'].includes(style.overflowY)) continue;
      const kind = element.getAttribute('data-scroll');
      if (kind !== 'column' && kind !== 'feed') undeclared.push(describe(element));
      if (style.overflowX !== 'hidden') wrongAxis.push(describe(element));
    }
    return {pageRegions, undeclared, wrongAxis};
  })()`);
}

/** A page owns exactly one declared column scroller; every other vertical scroller is declared too. */
export async function expectSingleDeclaredPageScroller(page: Page): Promise<void> {
  const report = await verticalScrollReport(page);
  expect(report.pageRegions, 'the main page column scroll region').toHaveLength(1);
  expect(
    report.undeclared,
    'vertical scrollers without data-scroll="column" or data-scroll="feed"',
  ).toEqual([]);
  expect(report.wrongAxis, 'vertical scrollers that do not clip their horizontal axis').toEqual([]);
}

/** Prove a deliberately long fixture has a real vertical range, not merely scroll styling. */
export async function expectPageColumnScrollable(page: Page): Promise<void> {
  const scrollable = await page.evaluate<boolean>(`(() => {
    const main = document.querySelector('main');
    if (main === null) return false;
    const column = main.matches('[data-scroll="column"]')
      ? main
      : main.querySelector(':scope > .vs-page-content > [data-scroll="column"]');
    return column !== null && column.scrollHeight > column.clientHeight + 1;
  })()`);
  expect(scrollable, 'the long Runs fixture exposes a vertical scrollbar').toBe(true);
}

/** Rendered regions that make the window or a vertical pane scroll sideways. */
export async function horizontalOverflow(page: Page): Promise<readonly string[]> {
  return page.evaluate<string[]>(`(() => {
    const describe = element => {
      const id = element.id === '' ? '' : '#' + element.id;
      const classes = Array.from(element.classList).map(name => '.' + name).join('');
      return element.tagName.toLowerCase() + id + classes;
    };
    const candidates = [
      document.documentElement,
      document.body,
      document.querySelector('#root'),
      document.querySelector('main'),
      ...document.querySelectorAll('[data-scroll="column"], [data-scroll="feed"]'),
    ].filter(element => element !== null);
    return Array.from(new Set(candidates))
      .filter(element => element.scrollWidth > element.clientWidth + 1)
      .map(element => describe(element) + ' (' + element.clientWidth + ' < ' + element.scrollWidth + ')');
  })()`);
}

/** The app window and every vertical pane fit horizontally at the current viewport. */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  expect(
    await horizontalOverflow(page),
    'horizontal overflow; wrap or clip the named region instead of widening the window',
  ).toEqual([]);
}

/** The semantics every top-level desktop destination exposes to keyboard and assistive-tech users. */
export async function expectAccessibleShell(
  page: Page,
  expected: {readonly heading: string; readonly activeDestination: string},
): Promise<void> {
  const main = page.getByRole('main');
  await expect(main, 'one main landmark').toHaveCount(1);
  const heading = main.getByRole('heading', {level: 1});
  await expect(heading, 'one level-one heading in main').toHaveCount(1);
  await expect(heading).toHaveText(expected.heading);

  const navigation = page.getByRole('navigation', {name: 'Main'});
  await expect(navigation, 'the named primary navigation').toHaveCount(1);
  for (const name of ['VibeSys', 'Runs', 'Cost', 'Machines', 'Settings']) {
    await expect(
      navigation.getByRole('button', {name, exact: true}),
      `${name} rail control`,
    ).toHaveCount(1);
  }
  const current = navigation.locator('[aria-current="page"]');
  await expect(current, 'one current rail destination').toHaveCount(1);
  await expect(current).toHaveAccessibleName(expected.activeDestination);
}
