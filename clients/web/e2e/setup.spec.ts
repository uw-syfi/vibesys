import {expect, type Page, test} from '@playwright/test';
import {runHref} from '../src/route.js';
import {FINISHED, mockGateway} from './gateway.js';
import {
  type FakeHome,
  FINISHED_RUN,
  GATEWAY_WS,
  HOME,
  LIVE_RUN,
  mockHome,
  OTHER_ROOT,
  PROJECT_ID,
  ROOT,
} from './home.js';

const sidebar = (page: Page) => page.getByRole('navigation', {name: 'Runs'});

test('the home page lists every project with its runs and links New run', async ({page}) => {
  await mockHome(page);
  await page.goto(HOME);
  await expect(page.getByText('Select a run, or choose New run in the sidebar')).toBeVisible();
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
  // mockGateway registers its /api/projects stub first so mockHome's later, broader /api/
  // handler wins for a request both would otherwise match.
  await mockGateway(page);
  await mockHome(page);
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
  await mockGateway(page);
  const home = await mockHome(page);
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
  // The home directory reads as ~; the canonical path is the hint.
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
  await footer.getByRole('button', {name: 'OpenAI API key is needed for Codex CLI'}).click();
  const key = page.getByLabel('OpenAI API key');
  await expect(key).toBeFocused();
  await key.fill(SECRET);
  await key.press('Enter');
  await expect(page.getByText('Saved in .env. Unverified until the first run.')).toBeVisible();
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
  await expect(page.locator('.sheetfoot')).toContainText('OpenAI API key was rejected');
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
  await expect(page.locator('button#f-key')).toHaveText('opencode auth login');
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
  await expect(picker.locator('h4')).toHaveAttribute('title', ROOT);
  await picker.getByRole('button', {name: 'Up'}).click();
  await expect(picker.locator('h4')).toHaveText('~/src');
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

test('the default folder is the most recent project that still exists, or none', async ({page}) => {
  await mockHome(page, {missing: [ROOT]});
  await page.goto(NEW);
  await expect(page.getByLabel('Folder')).toHaveValue(OTHER_ROOT);
  await page.unrouteAll();
  await mockHome(page, {missing: [ROOT, OTHER_ROOT]});
  await page.reload();
  await expect(page.getByRole('button', {name: 'Browse…'})).toBeVisible();
  await expect(page.getByLabel('Folder')).toHaveValue('');
});

test('Browse… from a folder that no longer exists lists the roots instead of a dead end', async ({
  page,
}) => {
  await mockHome(page);
  await page.goto(NEW);
  await expect(page.getByText('Git repository, working tree clean')).toBeVisible();
  const gone = '/Users/me/src/deleted';
  await page.getByLabel('Folder').fill(gone);
  await page.getByLabel('Folder').blur();
  await expect(page.getByText('Folder is not a git repository')).toBeVisible();
  await page.getByRole('button', {name: 'Browse…'}).click();
  const picker = page.getByRole('dialog');
  await expect(picker.getByRole('alert')).toContainText(`not a directory: ${gone}`);
  await picker.getByRole('button', {name: 'me', exact: true}).click();
  await picker.getByRole('button', {name: 'src', exact: true}).click();
  await picker.getByRole('button', {name: /tokenizer-rs/}).click();
  await picker.getByRole('button', {name: 'Choose this folder'}).click();
  await expect(picker).toHaveCount(0);
  await expect(page.getByLabel('Folder')).toHaveValue(OTHER_ROOT);
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
  await expect(picker.locator('h4')).toHaveAttribute('title', ROOT);
  await picker.getByRole('button', {name: 'Up'}).click();
  await expect(picker.locator('h4')).toHaveText('~/src');
  // Fires the slow fs(OTHER_ROOT); the displayed listing stays at /Users/me/src while it hangs.
  await picker.getByRole('button', {name: /tokenizer-rs/}).click();
  await picker.getByRole('button', {name: /llm-serve/}).click();
  await expect(picker.locator('h4')).toHaveAttribute('title', ROOT);
  home.releaseFs(OTHER_ROOT);
  await expect(picker.locator('h4')).toHaveAttribute('title', ROOT);
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
  await expect(picker.locator('h4')).toHaveText('~/src');
  await page.keyboard.press('ArrowDown');
  await expect(picker.getByRole('button', {name: /llm-serve/})).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(picker.getByRole('button', {name: /tokenizer-rs/})).toBeFocused();
  await page.keyboard.press('ArrowUp');
  await expect(picker.getByRole('button', {name: /llm-serve/})).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(picker.locator('h4')).toHaveAttribute('title', ROOT);
});

test('a new task is saved, committed after confirmation, and the folder becomes ready', async ({
  page,
}) => {
  const home = await mockHome(page, {validation: {state: 'no_tasks', pending: []}, tasks: []});
  await page.goto(NEW);
  await expect(page.getByText('No tasks yet. Create one below.')).toBeVisible();
  await expect(page.getByRole('button', {name: 'Save task'})).toBeDisabled();
  await page.getByLabel('Name').fill('decode-throughput');
  await page.getByLabel('Objective').fill('Increase decode throughput without changing outputs.');
  await page.getByLabel('Accuracy').fill('cargo test --release');
  await page.getByLabel('Benchmark').fill('cargo bench --bench decode');
  await page.getByLabel('Metric').fill('median_tok_per_sec');
  await expect(page.locator('.sheetfoot')).toContainText('Task has unsaved changes');
  await page.getByRole('button', {name: 'Save task'}).click();
  await expect(page.getByText('Task files are not committed.')).toBeVisible();
  await page.getByRole('button', {name: 'Commit task files…'}).click();
  const dialog = page.getByRole('dialog', {name: 'Commit the task files?'});
  await expect(dialog).toContainText('.vibesys/tasks/decode-throughput/OBJECTIVE.md');
  await dialog.getByRole('button', {name: 'Commit', exact: true}).click();
  await expect(page.getByText('Git repository, working tree clean')).toBeVisible();
  await expect(page.locator('.summary')).toContainText('decode-throughput');
  await expect(page.getByRole('button', {name: 'Start run'})).toBeEnabled();
  expect(posts(home, '/commit').map(request => request.body)).toEqual([
    {
      paths: [
        '.vibesys/tasks/decode-throughput/OBJECTIVE.md',
        '.vibesys/tasks/decode-throughput/vibesys.input.toml',
      ],
      message: null,
    },
  ]);
});

test('Cancel in the commit confirmation commits nothing', async ({page}) => {
  const home = await mockHome(page, {
    validation: {state: 'dirty_tree', pending: ['.vibesys/tasks/decode/OBJECTIVE.md']},
  });
  await page.goto(NEW);
  await page.getByRole('button', {name: 'Commit task files…'}).click();
  const dialog = page.getByRole('dialog', {name: 'Commit the task files?'});
  await expect(dialog.getByRole('button', {name: 'Cancel'})).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  expect(posts(home, '/commit')).toHaveLength(0);
});

test('a task file that appears after the preview is listed again before anything commits', async ({
  page,
}) => {
  const home = await mockHome(page, {
    validation: {state: 'dirty_tree', pending: ['.vibesys/tasks/decode/OBJECTIVE.md']},
    commitRace: true,
  });
  await page.goto(NEW);
  await page.getByRole('button', {name: 'Commit task files…'}).click();
  const dialog = page.getByRole('dialog', {name: 'Commit the task files?'});
  await dialog.getByRole('button', {name: 'Commit', exact: true}).click();
  await expect(dialog.getByRole('alert')).toHaveText(
    'The task files changed. Review the list again.',
  );
  await expect(dialog).toContainText('.vibesys/tasks/decode/profile.toml');
  await dialog.getByRole('button', {name: 'Commit', exact: true}).click();
  await expect(page.getByText('Git repository, working tree clean')).toBeVisible();
  expect(posts(home, '/commit').map(request => request.body)).toEqual([
    {paths: ['.vibesys/tasks/decode/OBJECTIVE.md'], message: null},
    {
      paths: ['.vibesys/tasks/decode/OBJECTIVE.md', '.vibesys/tasks/decode/profile.toml'],
      message: null,
    },
  ]);
});

test('Edit applies the form onto the saved task with its content hash', async ({page}) => {
  const home = await mockHome(page);
  await page.goto(NEW);
  await page.getByRole('button', {name: 'Edit'}).click();
  await expect(page.getByLabel('Name')).toHaveCount(0);
  await page.getByLabel('Benchmark').fill('cargo bench --bench decode -- --quick');
  await page.getByRole('button', {name: 'Save task'}).click();
  await expect(page.locator('.summary')).toContainText('cargo bench --bench decode -- --quick');
  const put = home.requests.find(request => request.method === 'PUT');
  expect(put?.path).toBe(`/api/projects/${PROJECT_ID}/tasks/decode`);
  expect(put?.body).toMatchObject({
    base_hash: 'h1',
    benchmark_command: 'cargo bench --bench decode -- --quick',
  });
});

test('an edit of a task that changed on disk is refused until the new version is loaded', async ({
  page,
}) => {
  const home = await mockHome(page, {changedOnDisk: true});
  await page.goto(NEW);
  await page.getByRole('button', {name: 'Edit'}).click();
  await page.getByLabel('Benchmark').fill('cargo bench --bench decode -- --quick');
  const save = page.getByRole('button', {name: 'Save task'});
  await save.click();
  await expect(page.getByRole('alert')).toHaveText(
    'The task changed on disk. Discard to load it, then edit again.',
  );
  // Save stays off, and the message stays through an edit, until Discard loads the new version.
  await expect(save).toBeDisabled();
  await page.getByLabel('Benchmark').fill('cargo bench --bench decode -- --quick --x');
  await expect(page.getByRole('alert')).toBeVisible();
  await page.getByRole('button', {name: 'Discard'}).click();
  await expect(page.locator('.summary')).toContainText('--features simd');
  await page.getByRole('button', {name: 'Edit'}).click();
  await expect(page.getByLabel('Benchmark')).toHaveValue(
    'cargo bench --bench decode --features simd',
  );
  await save.click();
  await expect(page.locator('.summary')).toBeVisible();
  const hashes = home.requests
    .filter(request => request.method === 'PUT')
    .map(request => (request.body as {base_hash: string}).base_hash);
  expect(hashes).toEqual(['h1', 'h2']);
});

test('a new task name is checked as it is typed, and an existing name is refused', async ({
  page,
}) => {
  await mockHome(page);
  await page.goto(NEW);
  await page.getByLabel('Task', {exact: true}).selectOption('+new');
  await page.getByLabel('Name').fill('Decode');
  // A field-state hint describes its field; only action results are alerts.
  await expect(page.getByLabel('Name')).toHaveAccessibleDescription(
    'Up to 128 of a-z, 0-9, ., _ or -, starting with a letter or digit.',
  );
  await expect(page.getByRole('alert')).toHaveCount(0);
  await page.getByLabel('Name').fill('decode');
  await expect(page.locator('#f-name-hint')).toHaveCount(0);
  await page.getByLabel('Objective').fill('Faster decode.');
  await page.getByLabel('Accuracy').fill('cargo test');
  await page.getByLabel('Benchmark').fill('cargo bench');
  await page.getByLabel('Metric').fill('tok_per_sec');
  await page.getByRole('button', {name: 'Save task'}).click();
  await expect(page.getByRole('alert')).toHaveText('A task with this name already exists.');
});

test('a launch failure shows the stderr tail with copyable locations; Retry sends again', async ({
  page,
  context,
}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const home = await mockHome(page, {start: 'launch_failed'});
  await page.goto(NEW);
  await page.getByRole('button', {name: 'Start run'}).click();
  await expect(page.locator('.titlebar')).toContainText('Did not start');
  await expect(page.locator('.claim')).toContainText('The run server exited with status 1');
  await page
    .getByRole('button', {name: 'File "/Users/me/vibesys/src/vibesys/config.py", line 214'})
    .click();
  await expect(page.getByText('Copied /Users/me/vibesys/src/vibesys/config.py:214')).toBeVisible();
  await page.getByRole('button', {name: 'Retry'}).click();
  await expect.poll(() => posts(home, '/runs').length).toBe(2);
  await page.getByRole('button', {name: 'Back to setup'}).click();
  await expect(page.getByLabel('Rounds')).toHaveValue('12');
});

test('a run server that dies while starting shows its stderr tail', async ({page}) => {
  await mockHome(page, {start: 'failed'});
  await page.goto(NEW);
  await page.getByRole('button', {name: 'Start run'}).click();
  await expect(page.locator('.titlebar')).toContainText('Did not start');
  await expect(page.getByRole('button', {name: 'benches/decode.rs:41:14'})).toHaveAttribute(
    'title',
    `Copy ${ROOT}/benches/decode.rs:41:14`,
  );
  await expect(page).not.toHaveURL(/gateway=/);
});

test('a finished run in the sidebar reopens read-only', async ({page}) => {
  await mockGateway(page, {through: FINISHED});
  const home = await mockHome(page);
  await page.goto(HOME);
  await sidebar(page)
    .getByRole('link', {name: /Reduce p99 prefill latency/})
    .click();
  await expect(page).toHaveURL(/gateway=/);
  await expect(page.locator('.titlebar')).toContainText('Completed');
  expect(posts(home, `/api/projects/${PROJECT_ID}/runs/${FINISHED_RUN}/open`)).toHaveLength(1);
});

test("Resume… from a finished run's menu resumes it; a smaller budget is refused", async ({
  page,
}) => {
  await mockGateway(page, {through: FINISHED});
  const home = await mockHome(page);
  await page.goto(runHref('home', PROJECT_ID, GATEWAY_WS));
  await expect(page.locator('.titlebar')).toContainText('Completed');
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitem', {name: 'Resume run…'}).click();
  await expect(page).toHaveURL(new RegExp(`#resume=${PROJECT_ID}/`));
  await expect(page.locator('.form')).toContainText('Recorded: 12 rounds.');
  await expect(page.getByLabel('Rounds')).toHaveAttribute('min', '12');
  await page.getByLabel('Rounds').fill('6');
  await page.getByRole('button', {name: 'Resume run'}).click();
  await expect(page.getByRole('alert')).toContainText(
    'The run already has a budget of 12; resume with at least that.',
  );
  await page.getByLabel('Rounds').fill('20');
  await page.getByRole('button', {name: 'Resume run'}).click();
  await expect(page).toHaveURL(/gateway=/);
  expect(posts(home, '/resume').map(request => request.body)).toEqual([{budget: 6}, {budget: 20}]);
});

test('a resume refused while another run is live names that run', async ({page}) => {
  await mockHome(page, {liveRun: LIVE_RUN});
  await page.goto(`${HOME}#resume=${PROJECT_ID}/${FINISHED_RUN}`);
  await page.getByRole('button', {name: 'Resume run'}).click();
  await expect(page.getByRole('alert')).toHaveText(
    `Run ${LIVE_RUN} is still live in this project; open it and stop it first.`,
  );
});

test('a live run offers no Resume', async ({page}) => {
  await mockGateway(page);
  await mockHome(page);
  await page.goto(runHref('home', PROJECT_ID, GATEWAY_WS));
  await page.getByRole('button', {name: 'More'}).click();
  await expect(page.getByRole('menuitem', {name: 'Resume run…'})).toHaveCount(0);
});

test('a 404 unknown_run, from a stale sidebar link, reads as a clear one-line message', async ({
  page,
}) => {
  await mockHome(page, {unknownRun: true});
  await page.goto(`${HOME}#open=${PROJECT_ID}/${FINISHED_RUN}`);
  await expect(page.getByRole('alert')).toHaveText('This run no longer exists.');
});

test('the home sidebar hides, returns and resizes as in the run window', async ({page}) => {
  await mockHome(page);
  await page.goto(NEW);
  await sidebar(page).getByRole('button', {name: 'Hide sidebar'}).click();
  await expect(sidebar(page)).toHaveCount(0);
  await page.locator('.titlebar').getByRole('button', {name: 'Show sidebar'}).click();
  const resizer = page.getByRole('separator', {name: 'Resize the sidebar'});
  await resizer.focus();
  await page.keyboard.press('ArrowRight');
  await expect(sidebar(page)).toHaveCSS('width', '292px');
});

test('the run list refreshes while the page is visible', async ({page}) => {
  await page.clock.install();
  const home = await mockHome(page);
  await page.goto(HOME);
  await expect(sidebar(page).getByText('Reduce p99 prefill latency')).toBeVisible();
  const lists = () => home.requests.filter(request => request.path === '/api/projects').length;
  const before = lists();
  await page.clock.runFor(5_000);
  await expect.poll(lists).toBeGreaterThan(before);
});

test("the home window's ••• switches the theme", async ({page}) => {
  await mockHome(page);
  await page.goto(`${HOME}#new`);
  const more = page.getByRole('button', {name: 'More'});
  await expect(more).toHaveAttribute('title', 'Theme');
  await more.click();
  await page.getByRole('menuitemradio', {name: 'Dark'}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.getByRole('menu')).toHaveCount(0);
  await expect(more).toBeFocused();
});
