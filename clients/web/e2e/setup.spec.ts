import {expect, type Page, test} from '@playwright/test';
import {runHref} from '../src/route.js';
import {mockGateway} from './gateway.js';
import {FINISHED_RUN, GATEWAY_WS, HOME, mockHome, PROJECT_ID} from './home.js';

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
