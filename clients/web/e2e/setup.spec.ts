import {expect, type Page, test} from '@playwright/test';
import {runHref} from '../src/route.js';
import {mockGateway} from './gateway.js';
import {
  type FakeHome,
  FINISHED_RUN,
  GATEWAY_WS,
  HOME,
  mockHome,
  OTHER_ROOT,
  PROJECT_ID,
  ROOT,
} from './home.js';

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
const posts = (home: FakeHome, suffix: string) =>
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

test('the home palette opens from its sidebar row and from ⌘K, and opens a run', async ({page}) => {
  await mockHome(page);
  await page.goto(HOME);
  await sidebar(page)
    .getByRole('button', {name: /Search and commands/})
    .click();
  const palette = page.getByRole('dialog', {name: 'Search and commands'});
  await expect(palette.getByRole('option', {name: /New run/})).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(palette).toHaveCount(0);
  await page.locator('.titlebar').click();
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.type('prefill');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(`/?token=home#open=${PROJECT_ID}/${FINISHED_RUN}`);
});

const SECRET = 'sk-e2e-0123456789';

test('a key is sent once in a PUT body and never shown back', async ({page}) => {
  const home = await mockHome(page);
  await page.goto(NEW);
  await page.getByRole('combobox', {name: 'Provider'}).selectOption('codex');
  const footer = page.locator('.sheetfoot');
  await footer.getByRole('button', {name: 'Codex CLI key is needed'}).click();
  const key = page.getByLabel('OpenAI API key');
  await expect(key).toBeFocused();
  await key.fill(SECRET);
  await key.press('Enter');
  await expect(page.getByText('Saved to .env. Unverified until the first run.')).toBeVisible();
  await expect(key).toHaveValue('');
  await expect(footer).not.toContainText('key is needed');
  expect(home.requests.filter(request => JSON.stringify(request).includes(SECRET))).toEqual([
    {method: 'PUT', path: '/api/auth/codex', body: {name: 'OPENAI_API_KEY', value: SECRET}},
  ]);
  expect(await page.content()).not.toContain(SECRET);
  const stored = await page.evaluate(
    () => JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage}) + document.cookie,
  );
  expect(stored).not.toContain(SECRET);
});

test('a rejected key keeps the field and says why', async ({page}) => {
  await mockHome(page, {rejectKey: true});
  await page.goto(NEW);
  await page.getByRole('combobox', {name: 'Provider'}).selectOption('codex');
  const key = page.getByLabel('OpenAI API key');
  await key.fill('sk-"bad');
  await page.getByRole('button', {name: 'Save', exact: true}).click();
  const alert = page.getByRole('alert').filter({hasText: 'Rejected: The key has a quote'});
  await expect(alert).toBeVisible();
  await expect(alert).not.toContainText('sk-"bad');
  await expect(key).toHaveValue('sk-"bad');
  await expect(page.locator('.sheetfoot')).toContainText('Codex CLI key is needed');
});

test('a provider change drops a typed key; a CLI-only provider shows its sign-in', async ({
  page,
}) => {
  await mockHome(page);
  await page.goto(NEW);
  const provider = page.getByRole('combobox', {name: 'Provider'});
  await provider.selectOption('codex');
  await page.getByLabel('OpenAI API key').fill(SECRET);
  await provider.selectOption('opencode');
  await expect(page.locator('code#f-key')).toHaveText('opencode auth login');
  await expect(page.locator('.sheetfoot')).toContainText('Sign in to OpenCode from a terminal');
  await provider.selectOption('codex');
  await expect(page.getByLabel('OpenAI API key')).toHaveValue('');
});

test('Browse… walks folders and checks the chosen one', async ({page}) => {
  const home = await mockHome(page);
  await page.goto(NEW);
  await expect(page.getByText('Git repository, working tree clean')).toBeVisible();
  await page.getByRole('button', {name: 'Browse…'}).click();
  const picker = page.getByRole('dialog');
  await expect(picker.locator('h4')).toHaveText(ROOT);
  await picker.getByRole('button', {name: 'Up'}).click();
  await expect(picker.locator('h4')).toHaveText('/Users/me/src');
  await picker.getByRole('button', {name: /tokenizer-rs/}).click();
  await picker.getByRole('button', {name: 'Choose this folder'}).click();
  await expect(picker).toHaveCount(0);
  await expect(page.getByLabel('Folder')).toHaveValue(OTHER_ROOT);
  await expect(page.getByText('No tasks yet. Create one below.')).toBeVisible();
  expect(
    home.requests.map(request => request.path).filter(path => path.startsWith('/api/fs')),
  ).toEqual([
    `/api/fs?path=${encodeURIComponent(ROOT)}`,
    '/api/fs?path=%2FUsers%2Fme%2Fsrc',
    `/api/fs?path=${encodeURIComponent(OTHER_ROOT)}`,
  ]);
});

test('a slow listing for a folder left behind is ignored once a newer one has landed', async ({
  page,
}) => {
  // ROOT is used for the initial (fast) load, so the slow path must be one only reached mid-navigation.
  const home = await mockHome(page, {slowFs: [OTHER_ROOT]});
  await page.goto(NEW);
  await expect(page.getByText('Git repository, working tree clean')).toBeVisible();
  await page.getByRole('button', {name: 'Browse…'}).click();
  const picker = page.getByRole('dialog');
  await expect(picker.locator('h4')).toHaveText(ROOT);
  await picker.getByRole('button', {name: 'Up'}).click();
  await expect(picker.locator('h4')).toHaveText('/Users/me/src');
  // Fires the slow fs(OTHER_ROOT); the displayed listing stays at /Users/me/src while it hangs.
  await picker.getByRole('button', {name: /tokenizer-rs/}).click();
  await picker.getByRole('button', {name: /llm-serve/}).click();
  await expect(picker.locator('h4')).toHaveText(ROOT);
  home.releaseFs(OTHER_ROOT);
  await expect(picker.locator('h4')).toHaveText(ROOT);
  await expect(picker.getByText('No folders here.')).toBeVisible();
});

test('closing the picker returns focus to Browse…, on cancel, escape, and choose', async ({
  page,
}) => {
  await mockHome(page);
  await page.goto(NEW);
  await expect(page.getByText('Git repository, working tree clean')).toBeVisible();
  const browse = page.getByRole('button', {name: 'Browse…'});
  const picker = page.getByRole('dialog');

  await browse.click();
  await expect(picker).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(picker).toHaveCount(0);
  await expect(browse).toBeFocused();

  await browse.click();
  await picker.getByRole('button', {name: 'Cancel'}).click();
  await expect(picker).toHaveCount(0);
  await expect(browse).toBeFocused();

  await browse.click();
  await picker.getByRole('button', {name: 'Up'}).click();
  await picker.getByRole('button', {name: /tokenizer-rs/}).click();
  await picker.getByRole('button', {name: 'Choose this folder'}).click();
  await expect(picker).toHaveCount(0);
  await expect(browse).toBeFocused();
});

test('arrow keys move focus over the picker rows; Enter opens the focused one', async ({page}) => {
  await mockHome(page);
  await page.goto(NEW);
  await expect(page.getByText('Git repository, working tree clean')).toBeVisible();
  await page.getByRole('button', {name: 'Browse…'}).click();
  const picker = page.getByRole('dialog');
  await picker.getByRole('button', {name: 'Up'}).click();
  await expect(picker.locator('h4')).toHaveText('/Users/me/src');
  await page.keyboard.press('ArrowDown');
  await expect(picker.getByRole('button', {name: /llm-serve/})).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(picker.getByRole('button', {name: /tokenizer-rs/})).toBeFocused();
  await page.keyboard.press('ArrowUp');
  await expect(picker.getByRole('button', {name: /llm-serve/})).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(picker.locator('h4')).toHaveText(ROOT);
});
