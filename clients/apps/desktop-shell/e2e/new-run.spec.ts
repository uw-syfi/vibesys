import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import type {HarnessOffer} from '@vibesys/ipc';
import {rawTextOf} from './checks/screen-check.js';
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
  await expect(page.getByLabel('Run on')).toHaveValue('ssh:host-b');
  await expect(page.getByLabel('Project', {exact: true})).toHaveValue(PROJECT);
  await expect(page.getByLabel('Task', {exact: true})).toHaveValue('mpmc');
  await expect(page.getByRole('radio', {name: 'Claude Code'})).toBeChecked();
  await expect(page.getByRole('slider', {name: 'Model'})).toHaveAttribute('aria-valuetext', 'Opus');
  await expect(page.getByText('Opus 4.5', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: 'Show all models'}).click();
  await expect(page.getByLabel('Model', {exact: true})).toHaveValue('claude-opus-4-5');
  await expect(page.getByLabel('Model', {exact: true}).locator('option:checked')).toHaveText(
    'Opus 4.5',
  );
  for (const oldHint of [
    'Runs on CPU, as the task declares.',
    'Only models VibeSys has tested with this harness are listed.',
    'Searches without rounds, such as evolve, take none.',
  ]) {
    await expect(page.getByText(oldHint, {exact: true})).toHaveCount(0);
  }
  // The checks are visible in the side panel and can be opened on narrow screens.
  await expect(ready(page)).toContainText(/Ready · \d+ checks passed/);
  const toggle = ready(page).getByRole('button', {name: /Ready/});
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
  await expect(ready(page)).toContainText('Agent image');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);

  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  await expect(page.getByText('host-b · 20261015-200000')).toBeVisible();
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

test('the model tiers choose a vetted model, and the full list can choose a version', async ({
  page,
}) => {
  await open(page);
  const slider = page.getByRole('slider', {name: 'Model'});
  await expect(slider).toHaveAttribute('aria-valuetext', 'Opus');
  await slider.press('ArrowLeft');
  await expect(slider).toHaveAttribute('aria-valuetext', 'Sonnet');
  await expect(page.getByText('Sonnet 4.5', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: 'Show all models'}).click();
  await expect(page.getByLabel('Model', {exact: true})).toHaveValue('claude-sonnet-4-5');
  await page.getByLabel('Model', {exact: true}).selectOption('claude-opus-4-5');
  await page.getByRole('button', {name: 'Show model tiers'}).click();
  await expect(slider).toHaveAttribute('aria-valuetext', 'Opus');
});

test('the model tiers are no wider than the selectors on the page', async ({page}) => {
  await page.setViewportSize({width: 1920, height: 1000});
  await open(page);
  const widths = await page.evaluate(() => {
    const aside = Number.parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue('--column-aside'),
    );
    const width = (selector: string) =>
      document.querySelector(selector)?.getBoundingClientRect().width;
    return {cap: aside * 1.8, slider: width('.vs-slider'), select: width('.vs-select')};
  });
  expect(widths.slider).toBeGreaterThan(0);
  expect(widths.slider).toBeLessThanOrEqual(widths.cap);
  expect(widths.select).toBeLessThanOrEqual(widths.cap);
});

test('the checks start open beside the form and closed under it, so Start run stays in view', async ({
  page,
}) => {
  const toggle = ready(page).getByRole('button', {name: /Ready/});
  await page.setViewportSize({width: 800, height: 900});
  await open(page);
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(ready(page).getByRole('region', {name: 'Details'})).toHaveCount(0);
  await expect(start(page)).toBeVisible();
  await expect(page.getByLabel('Task', {exact: true})).toBeInViewport();
  await toggle.click();
  await expect(ready(page).getByRole('region', {name: 'Details'})).toBeVisible();

  await page.setViewportSize({width: 1440, height: 900});
  await page.reload();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
});

test('a helper without model tiers keeps the full vetted model list', async ({page}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await open(page, {
    ...SEED,
    newRun: {
      ...newRun,
      harnesses: (newRun.harnesses as readonly HarnessOffer[]).map(({display, ...offer}) => ({
        ...offer,
        display: display === undefined ? undefined : {providerName: display.providerName},
      })),
    },
  });
  await expect(page.getByRole('slider', {name: 'Model'})).toHaveCount(0);
  await expect(page.getByLabel('Model', {exact: true})).toHaveValue('claude-opus-4-5');
  await expect(page.getByRole('button', {name: 'Show all models'})).toHaveCount(0);
});

test('a project inside another repository is copied out by its fix, then may start', async ({
  page,
}) => {
  await open(page);
  await page.getByRole('radio', {name: 'Open a folder'}).check();
  await page.getByLabel('Folder on host-b').fill('/home/me/vibesys/examples/queue-rs');
  await page.getByRole('button', {name: 'Open', exact: true}).click();
  const copy = ready(page).getByRole('button', {name: 'Copy to its own repository'});
  await expect(copy).toBeVisible();
  await expect(ready(page)).toContainText('Its copy goes to /home/me/vibesys-projects/queue-rs-2.');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await expect(ready(page)).toContainText('Fix the items marked above to start.');

  await copy.click();
  await expect(copy).toHaveCount(0);
  await expect(ready(page)).toContainText('queue-rs-2 · mpmc · host-b');
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
  await page.getByRole('button', {name: 'More options'}).click();
  await page.getByLabel('Extra arguments').fill('--max-rounds 3');
  await expect(page.getByText('--max-rounds is set above')).toHaveCount(2);
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await page.getByLabel('Extra arguments').fill('--max-in-flight 3');
  await page.getByLabel('Rounds').selectOption('5');
  await page.getByRole('button', {name: /^Roles:/}).click();
  await page.getByLabel('Judge reasoning effort').selectOption('high');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  await expect(page.getByText('host-b · 20261015-200000')).toBeVisible();
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
    'VibeSys on host-b is older than this app: update its checkout to start runs from here ' +
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
  await page.getByRole('button', {name: 'More options'}).click();
  await expect(page.getByText(/does not report its agent roles/)).toBeVisible();
  await expect(page.getByRole('button', {name: /^Roles:/})).toHaveCount(0);
});

test('the folder field completes paths on the machine', async ({page}) => {
  await open(page);
  await page.getByRole('radio', {name: 'Open a folder'}).check();
  const field = page.getByLabel('Folder on host-b');
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
  await expect(page.getByLabel('Run on')).toHaveValue('ssh:host-b');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: 'Settings'})
    .click();
  await page
    .getByRole('navigation', {name: 'Settings sections'})
    .getByRole('button', {name: /Machines/})
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
  await expect(page.getByRole('slider', {name: 'Model'})).toHaveAttribute(
    'aria-valuetext',
    'Sonnet',
  );
  await expect(page.getByText('Sonnet 4.5', {exact: true})).toBeVisible();
  await expect(page.getByLabel('Rounds')).toHaveValue('4');
  // Extra arguments are in More options, which says they are set while it is closed.
  await expect(page.getByRole('button', {name: /More options/})).toContainText('extra arguments');
  await page.getByRole('button', {name: 'More options'}).click();
  await expect(page.getByLabel('Extra arguments')).toHaveValue('--max-in-flight 3');
});

test('the harness is chosen from tiles with a mark and a name each', async ({page}) => {
  await open(page);
  const harness = page.getByRole('radiogroup', {name: 'Harness'});
  await expect(harness.getByRole('radio')).toHaveCount(2);
  // A harness with a mark in the package draws it; one without draws the generic glyph.
  await expect(harness.locator('svg.vs-brand-mark')).toHaveCount(2);
  await expect(harness.locator('svg.vs-brand-mark[data-generic]')).toHaveCount(1);
  await page.getByRole('radio', {name: 'Codex'}).check();
  await expect(page.getByLabel('Model', {exact: true})).toHaveValue('gpt-5-codex');
  await expect(page.getByLabel('Model', {exact: true}).locator('option:checked')).toHaveText(
    'GPT-5 Codex',
  );
  await start(page).click();
  const {started} = await fakeNewRun(page);
  expect(started).toMatchObject([{harness: {provider: 'codex', model: 'gpt-5-codex'}}]);
});

test('More options starts closed, says what it holds, and stays as left on each machine', async ({
  page,
}) => {
  await open(page);
  const more = page.getByRole('button', {name: /More options/});
  await expect(more).toHaveAttribute('aria-expanded', 'false');
  await expect(more).toContainText('Defaults');
  await expect(page.getByLabel('Extra arguments')).toHaveCount(0);
  await more.click();
  await expect(page.getByLabel('Extra arguments')).toBeVisible();
  // Left open on this machine, it is open on the next visit; another machine has its own choice.
  await page.reload();
  await expect(page.getByRole('button', {name: /More options/})).toHaveAttribute(
    'aria-expanded',
    'true',
  );
  await page.getByLabel('Run on').selectOption('local');
  await expect(page.getByRole('button', {name: /More options/})).toHaveAttribute(
    'aria-expanded',
    'false',
  );
});

test('a failing check opens the checks by itself, and the error stays when More options is closed', async ({
  page,
}) => {
  await open(page);
  await page.getByRole('button', {name: 'More options'}).click();
  await page.getByLabel('Extra arguments').fill('--max-rounds 3');
  await page.getByRole('button', {name: 'More options'}).click();
  await expect(page.getByLabel('Extra arguments')).toHaveCount(0);
  // Closed, it still shows the problem, and the pinned panel states and explains it.
  await expect(page.getByRole('button', {name: /More options/})).toContainText(
    '--max-rounds is set above',
  );
  await expect(ready(page)).toContainText('1 check needs attention');
  await expect(ready(page)).toContainText('--max-rounds is set above');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
});

test('New run, with More options open, draws no role, harness or model ids as text', async ({
  page,
}) => {
  await open(page);
  await page.getByRole('button', {name: /More options/}).click();
  await expect(page.getByLabel('Extra arguments')).toBeVisible();
  // Agentshim supplies harness and model names; the raw ids stay in option values.
  expect(await rawTextOf(page)).toEqual([]);
});
