/**
 * New run, an example whose destination already holds something: the Review page offers the
 * action that fits (create, use the project that is there, or another name), never a Create that
 * would fail, and says nothing about a home directory.
 */
import {expect, type Page, test} from '@playwright/test';
import {REQUIRED_HELPER_FEATURES} from '@vibesys/hosts';
import {main, pickExample} from './new-run-flow.js';
import {type FakeHostWindow, openNewRun as open, SEED} from './world.js';

const start = (page: Page) => main(page).getByRole('button', {name: 'Start run'});
const DESTINATION = '~/vibesys-projects/stack';

/** Open New run on a helper that answers `destinations`, and answer the stack example through to Review. */
async function reviewTheStack(
  page: Page,
  destinations: Readonly<Record<string, {state: string; project: string | null}>>,
): Promise<void> {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  const seed = {
    ...SEED,
    newRun: {
      ...newRun,
      destinations,
      features: [
        ...REQUIRED_HELPER_FEATURES,
        'run_roles',
        'example_catalogue',
        'example_destination',
      ],
    },
  };
  await open(page, seed);
  await pickExample(page, 'stack', 'mpmc');
}

async function startedProjects(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    (window as unknown as FakeHostWindow).fakeHost.newRun.started.map(
      run => (run as {project: string}).project,
    ),
  );
}

test('a destination that is taken offers another name, and a free one creates', async ({page}) => {
  await reviewTheStack(page, {[DESTINATION]: {state: 'occupied', project: null}});
  await expect(main(page)).toContainText(`${DESTINATION} is taken`);
  await expect(main(page).getByRole('button', {name: 'Create the project'})).toHaveCount(0);
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await main(page).getByRole('button', {name: 'Choose another name'}).click();
  const field = main(page).getByLabel('Create the example at');
  await expect(field).toHaveValue(DESTINATION);
  await expect(main(page)).toContainText('Taken');
  await field.fill('~/vibesys-projects/stack-2');
  await main(page).getByRole('button', {name: 'Create the project'}).click();
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect.poll(() => startedProjects(page)).toEqual(['~/vibesys-projects/stack-2']);
});

test('the example project already there is used as it is', async ({page}) => {
  await reviewTheStack(page, {[DESTINATION]: {state: 'example_project', project: DESTINATION}});
  await expect(main(page).getByRole('button', {name: 'Create the project'})).toHaveCount(0);
  await main(page).getByRole('button', {name: 'Use it'}).click();
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect.poll(() => startedProjects(page)).toEqual([DESTINATION]);
});

test('an empty directory is created in, as an absent one is', async ({page}) => {
  await reviewTheStack(page, {[DESTINATION]: {state: 'empty', project: null}});
  await main(page).getByRole('button', {name: 'Create the project'}).click();
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
});
