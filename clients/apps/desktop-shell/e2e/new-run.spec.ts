import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {installFakeBridge, installFakeMachines, installFakeNewRun} from './fake-bridge.js';
import {installFakeRun} from './fake-run.js';
import {type FakeHostWindow, open, PROJECT, SEED} from './world.js';

const ready = (page: Page) => page.getByRole('region', {name: 'Ready to start'});
const start = (page: Page) => ready(page).getByRole('button', {name: 'Start run'});

async function fakeNewRun(page: Page) {
  return page.evaluate(() => (window as unknown as FakeHostWindow).fakeHost.newRun);
}

test('New run is prefilled from the last run on its machine and starts once the checks pass', async ({
  page,
}) => {
  await open(page);
  await expect(page.getByRole('heading', {name: 'New run'})).toBeVisible();
  await expect(page.getByLabel('Run on')).toHaveValue('ssh:chelan4');
  await expect(page.getByLabel('Project', {exact: true})).toHaveValue(PROJECT);
  await expect(page.getByLabel('Task', {exact: true})).toHaveValue('mpmc');
  await expect(page.getByLabel('Model', {exact: true})).toHaveValue('claude-opus-4-5');
  await expect(ready(page)).toContainText('Agent image');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);

  await start(page).click();
  await expect(page.getByRole('tablist', {name: 'Run views'})).toBeVisible();
  await expect(page.getByText('chelan4 · 20261015-200000')).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toEqual([
    {
      project: PROJECT,
      task: 'mpmc',
      harness: {provider: 'claude', model: 'claude-opus-4-5'},
      options: {results: 'keep', roles: []},
      args: [],
    },
  ]);
});

test('a project inside another repository is copied out by its fix, then may start', async ({
  page,
}) => {
  await open(page);
  await page.getByRole('radio', {name: 'Open a folder'}).check();
  await page.getByLabel('Folder on chelan4').fill('/home/me/vibesys/examples/queue-rs');
  await page.getByRole('button', {name: 'Open', exact: true}).click();
  const copy = ready(page).getByRole('button', {name: 'Copy to its own repository'});
  await expect(copy).toBeVisible();
  await expect(ready(page)).toContainText('Its copy goes to /home/me/vibesys-projects/queue-rs-2.');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await expect(ready(page)).toContainText('Fix the items marked above to start.');

  await copy.click();
  await expect(copy).toHaveCount(0);
  await expect(ready(page)).toContainText('queue-rs-2 · mpmc · chelan4');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
});

test('an example becomes a project of its own before the run is checked', async ({page}) => {
  await open(page);
  await page.getByRole('radio', {name: 'From an example'}).check();
  await page.getByLabel('Example', {exact: true}).selectOption('kv-store');
  const create = ready(page).getByRole('button', {name: 'Create the project'});
  await expect(create).toBeVisible();
  await expect(ready(page)).toContainText('at ~/vibesys-projects/kv-store first');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await create.click();
  await expect(page.getByLabel('Task', {exact: true})).toHaveValue('get-put');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
});

test('extra arguments may not repeat an option set above, and roles change the run', async ({
  page,
}) => {
  await open(page);
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await page.getByRole('button', {name: 'Advanced'}).click();
  await page.getByLabel('Extra arguments').fill('--max-rounds 3');
  await expect(page.getByText('--max-rounds is set above')).toHaveCount(2);
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await page.getByLabel('Extra arguments').fill('--max-in-flight 3');
  await page.getByLabel('Rounds').selectOption('5');
  await page.getByRole('button', {name: /^Roles:/}).click();
  await page.getByLabel('Multi judge reasoning effort').selectOption('high');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('tablist', {name: 'Run views'})).toBeVisible();
  await expect(page.getByText('chelan4 · 20261015-200000')).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toEqual([
    {
      project: PROJECT,
      task: 'mpmc',
      harness: {provider: 'claude', model: 'claude-opus-4-5'},
      options: {
        rounds: 5,
        results: 'keep',
        roles: [{role: 'multi-judge', model: null, reasoning_effort: 'high'}],
      },
      args: ['--max-in-flight', '3'],
    },
  ]);
});

test('a machine whose VibeSys predates New run says so and is never asked to start', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await open(page, {...SEED, newRun: {...newRun, features: ['harnesses']}});
  await expect(ready(page)).toContainText(
    'VibeSys on chelan4 is older than this app: update its checkout to start runs from here ' +
      '(it lacks run_options, preflight, examples).',
  );
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByRole('radio', {name: 'From an example'})).toHaveCount(0);
  await start(page).click({force: true});
  const {started, preflights} = await fakeNewRun(page);
  expect(started).toEqual([]);
  expect(preflights).toEqual([]);
});

test('a machine whose VibeSys does not report its roles says every role runs as the run', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await open(page, {
    ...SEED,
    newRun: {...newRun, features: ['run_options', 'preflight', 'examples']},
  });
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByText(/does not report its agent roles/)).toBeVisible();
  await expect(page.getByRole('button', {name: /^Roles:/})).toHaveCount(0);
});

test('the folder field completes paths on the machine', async ({page}) => {
  await open(page);
  await page.getByRole('radio', {name: 'Open a folder'}).check();
  const field = page.getByLabel('Folder on chelan4');
  await field.fill('/home/me/pro');
  await page.getByRole('option', {name: '/home/me/projects/'}).click();
  await expect(field).toHaveValue('/home/me/projects/');
  await page.getByRole('option', {name: '/home/me/projects/queue-rs/'}).click();
  await expect(field).toHaveValue('/home/me/projects/queue-rs/');
  await page.getByRole('button', {name: 'Open', exact: true}).click();
  await expect(page.getByLabel('Task', {exact: true})).toHaveValue('mpmc');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
});

test("Machines' New run here opens New run on that machine", async ({page}) => {
  await open(page);
  await expect(page.getByLabel('Run on')).toHaveValue('ssh:chelan4');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: /^Machines/})
    .click();
  await page
    .getByRole('tree', {name: 'Machines'})
    .getByRole('treeitem', {name: 'This Mac'})
    .click();
  await page.getByRole('button', {name: 'New run here'}).click();
  await expect(page.getByRole('heading', {name: 'New run'})).toBeVisible();
  await expect(page.getByLabel('Run on')).toHaveValue('local');
});

test('Run again prefills the model, rounds and extra arguments the run was started with', async ({
  page,
}) => {
  const start = {
    harness: {provider: 'claude', model: 'claude-sonnet-4-5'},
    options: {rounds: 4, time_limit_s: null, results: 'keep', roles: []},
    args: ['--max-in-flight', '3'],
  };
  const recent = (SEED.recent ?? []).map(run =>
    (run as {runId: string}).runId === '20261014-090000' ? {...(run as object), start} : run,
  );
  const seed = {...SEED, recent};
  await page.addInitScript(installFakeBridge, seed);
  await page.addInitScript(installFakeMachines, seed);
  await page.addInitScript(installFakeNewRun, seed);
  await page.addInitScript(installFakeRun, {streams: [], text: {}});
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: /^\S+ queue-rs · spsc/})
    .click();
  await page.getByRole('button', {name: 'Run again', exact: true}).click();
  await expect(page.getByRole('heading', {name: 'New run'})).toBeVisible();
  await expect(page.getByLabel('Model', {exact: true})).toHaveValue('claude-sonnet-4-5');
  await expect(page.getByLabel('Rounds')).toHaveValue('4');
  await expect(page.getByLabel('Extra arguments')).toHaveValue('--max-in-flight 3');
});
