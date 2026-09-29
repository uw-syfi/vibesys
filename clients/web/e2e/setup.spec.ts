import {expect, type Page, test} from '@playwright/test';
import {runHref} from '../src/route.js';
import {mockGateway} from './gateway.js';
import {FINISHED_RUN, GATEWAY_WS, HOME, mockHome, OTHER_ROOT, PROJECT_ID, ROOT} from './home.js';

const sidebar = (page: Page) => page.getByRole('navigation', {name: 'Runs'});

test('the home page lists every project with its runs and links New run', async ({page}) => {
  await mockHome(page);
  await page.goto(HOME);
  await expect(page.getByText('Select a run, or start one with ⌘N')).toBeVisible();
  const llm = sidebar(page).getByRole('region', {name: 'llm-serve'});
  await expect(llm.getByRole('link', {name: /Reduce p99 prefill latency/})).toHaveAttribute(
    'href',
    `/?token=home#open=${PROJECT_ID}/${FINISHED_RUN}`,
  );
  await expect(llm.getByRole('link', {name: /Increase decode throughput/})).toHaveAttribute(
    'href',
    /gateway=/,
  );
  await expect(sidebar(page).getByRole('region', {name: 'tokenizer-rs'})).toContainText(
    'Speed up the BPE merge loop',
  );
  await expect(sidebar(page).getByRole('link', {name: /New run/})).toHaveAttribute(
    'href',
    '/?token=home#new',
  );
});

test('a run page opened from the home links New run, also on ⌘N', async ({page}) => {
  await mockHome(page);
  await mockGateway(page);
  await page.goto(runHref('home', PROJECT_ID, GATEWAY_WS));
  await expect(page.locator('.titlebar')).toContainText('Judging round 6');
  await expect(sidebar(page).getByRole('link', {name: /New run/})).toHaveAttribute(
    'href',
    '/?token=home#new',
  );
  await page.locator('.titlebar').click();
  await page.keyboard.press('ControlOrMeta+n');
  await expect(page).toHaveURL(/#new$/);
});

test('a home link whose token the home no longer accepts says so instead of opening a dead run page', async ({
  page,
}) => {
  await page.route(/\/api\//, route =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({
        error: {
          code: 'unauthorized',
          message: 'missing or invalid capability token',
          details: null,
        },
      }),
    }),
  );
  await page.goto('/?token=stale');
  await expect(
    page.getByText('This link is no longer valid. Open the URL `vibesys web home` prints.'),
  ).toBeVisible();
  await expect(page.locator('.titlebar')).toHaveCount(0);
});

test("a gateway's own page, whose origin has no home API, still opens its run", async ({page}) => {
  await page.route(/\/api\//, route => route.fulfill({status: 404, body: 'not found'}));
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await expect(page.locator('.titlebar')).toContainText('Judging round 6');
  await expect(sidebar(page).getByRole('link', {name: /New run/})).toHaveCount(0);
});

const NEW = `${HOME}#new`;
const posts = (home: {requests: {method: string; path: string}[]}, suffix: string) =>
  home.requests.filter(request => request.method === 'POST' && request.path.endsWith(suffix));

test('a ready folder with a saved task starts a run and opens it once it attaches', async ({
  page,
}) => {
  const home = await mockHome(page);
  await mockGateway(page);
  await page.goto(NEW);
  await expect(page.getByText('Git repository, working tree clean')).toBeVisible();
  await expect(page.locator('.summary')).toContainText('cargo bench --bench decode');
  await page.getByRole('button', {name: 'Start run'}).click();
  await expect(page).toHaveURL(/gateway=/);
  await expect(page.locator('.titlebar')).toContainText('Judging round 6');
  expect(posts(home, `/api/projects/${PROJECT_ID}/runs`).map(request => request.body)).toEqual([
    {
      task: 'decode',
      outer_loop: 'agent',
      budget: 12,
      compute_backend: 'metal',
      driver: null,
      provider: 'claude',
      model: 'claude-opus-5',
      reasoning_effort: null,
      roles: {},
    },
  ]);
});

test('each blocker links to the field that fixes it', async ({page}) => {
  await mockHome(page, {validation: {state: 'dirty_tree', pending: ['src/batch.rs']}});
  await page.goto(NEW);
  await expect(page.getByText('Uncommitted changes in src/batch.rs.')).toBeVisible();
  await page.getByLabel('Rounds').fill('');
  const footer = page.locator('.sheetfoot');
  await expect(footer).toContainText('2 to fix:');
  await expect(page.getByRole('button', {name: 'Start run'})).toBeDisabled();
  await footer.getByRole('button', {name: 'Rounds must be a whole number'}).click();
  await expect(page.getByLabel('Rounds')).toBeFocused();
  await page.getByLabel('Rounds').fill('12');
  const advanced = page.locator('summary', {hasText: 'Advanced'});
  await advanced.click();
  await page.getByLabel('Outer loop').selectOption('profile-guided');
  await advanced.click();
  await footer
    .getByRole('button', {name: 'Profile-guided needs a task with a [profile_guided] section'})
    .click();
  await expect(page.getByLabel('Outer loop')).toBeFocused();
});

test('Start sends one request however often it is clicked', async ({page}) => {
  const home = await mockHome(page, {hold: ['attach']});
  await page.goto(NEW);
  const start = page.getByRole('button', {name: 'Start run'});
  await expect(start).toBeEnabled();
  await start.dblclick();
  await expect(page.locator('.sheetfoot')).toContainText('Starting the run…');
  expect(posts(home, '/runs')).toHaveLength(1);
});

test('a slow check of an older folder never replaces a newer one', async ({page}) => {
  const home = await mockHome(page, {slow: [ROOT]});
  await page.goto(NEW);
  await expect(page.getByText('Checking…')).toBeVisible();
  const folder = page.getByLabel('Folder');
  await folder.fill(OTHER_ROOT);
  await folder.press('Enter');
  await expect(page.getByText('No tasks yet. Create one below.')).toBeVisible();
  const answered = page.waitForResponse(response =>
    response.url().endsWith('/api/projects/validate'),
  );
  home.release(ROOT);
  await answered;
  await expect(page.getByText('No tasks yet. Create one below.')).toBeVisible();
  await expect(page.getByText('Git repository, working tree clean')).toHaveCount(0);
  await expect(folder).toHaveValue(OTHER_ROOT);
});
