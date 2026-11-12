import {expect, type Page} from '@playwright/test';

/** A screen reached through controls a person uses, never through product internals. */
export interface FixtureScreen {
  readonly slug: string;
  readonly heading: string;
  readonly activeDestination: string;
  open(page: Page): Promise<void>;
}

/** Long names and enough live and ended runs to exercise the app-window layout. */
export const FIXTURE_PATH =
  '/fixture.html?scenario=11-12-live-tree-3&fleet=30-10&long&host=local&host=ssh%3Ahost-a';

async function openRail(page: Page, name: string): Promise<void> {
  const navigation = page.getByRole('navigation', {name: 'Main'});
  const control = navigation.getByRole('button', {name, exact: true});
  await expect(control).toBeVisible();
  await control.click();
  await expect(control).toHaveAttribute('aria-current', 'page');
}

async function expectScreen(page: Page, heading: string): Promise<void> {
  const main = page.getByRole('main');
  await expect(main).toHaveAttribute('data-state', 'ready');
  await expect(main.getByRole('heading', {level: 1, name: heading, exact: true})).toBeVisible();
}

/** Open the fixture and wait on its own ready landmark, not on network idleness or elapsed time. */
export async function openFixture(page: Page): Promise<void> {
  await page.goto(FIXTURE_PATH);
  await expect(page.getByRole('navigation', {name: 'Main'})).toBeVisible();
  await expectScreen(page, 'Runs');
}

const runs: FixtureScreen = {
  slug: 'runs',
  heading: 'Runs',
  activeDestination: 'Runs',
  open: async page => {
    await openRail(page, 'Runs');
    await expectScreen(page, 'Runs');
  },
};

const newRun: FixtureScreen = {
  slug: 'new-run',
  heading: 'New run',
  activeDestination: 'Runs',
  open: async page => {
    await runs.open(page);
    const control = page.getByRole('button', {name: 'New run', exact: true});
    await expect(control).toBeVisible();
    await control.click();
    await expectScreen(page, 'New run');
  },
};

async function openNewRunChoice(page: Page, name: RegExp, heading: string): Promise<void> {
  await newRun.open(page);
  const control = page.getByRole('button', {name});
  await expect(control).toBeVisible();
  await control.click();
  await expectScreen(page, heading);
}

const catalog: FixtureScreen = {
  slug: 'catalog',
  heading: 'Start from an example',
  activeDestination: 'Runs',
  open: page => openNewRunChoice(page, /Start from an example/, 'Start from an example'),
};

const own: FixtureScreen = {
  slug: 'my-own',
  heading: 'Bring your own',
  activeDestination: 'Runs',
  open: page => openNewRunChoice(page, /Bring a repository, or describe one/, 'Bring your own'),
};

const addMachine: FixtureScreen = {
  slug: 'add-machine',
  heading: 'Add machine or server',
  activeDestination: 'Runs',
  open: async page => {
    await newRun.open(page);
    const control = page.getByRole('button', {name: 'Add machine or server', exact: true});
    await expect(control).toBeVisible();
    await control.click();
    await expectScreen(page, 'Add machine or server');
  },
};

function railScreen(slug: string, heading: string): FixtureScreen {
  return {
    slug,
    heading,
    activeDestination: heading,
    open: async page => {
      await openRail(page, heading);
      await expectScreen(page, heading);
    },
  };
}

/** The approval journey: Runs, its start page, then every other rail destination. */
export const FIXTURE_SCREENS: readonly FixtureScreen[] = [
  runs,
  newRun,
  catalog,
  own,
  addMachine,
  railScreen('cost', 'Cost'),
  railScreen('machines', 'Machines'),
  railScreen('settings', 'Settings'),
];
