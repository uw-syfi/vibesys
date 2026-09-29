import {expect, type Page, test} from '@playwright/test';
import {FINISHED, mockGateway} from './gateway.js';

const rounds = (page: Page) => page.getByRole('navigation', {name: 'Runs'});
const round = (page: Page, n: number) =>
  rounds(page).getByRole('button', {name: new RegExp(`^Round ${n},`)});

test('the live round: the acting judge, the kept checkpoint, the sidebar rounds', async ({
  page,
}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await expect(round(page, 6)).toHaveAttribute('aria-current', 'true');
  await expect(rounds(page).getByText('6 more planned')).toBeVisible();
  await expect(page.locator('.titlebar')).toContainText('Judging round 6');
  await expect(page.locator('.titlebar .kept')).toContainText('Retained 1,230');
  await expect(page.getByRole('region', {name: 'Judge, Attempt 1'})).toContainText(
    'Checking the change against the pass criteria',
  );
});

test('a finished round shows its verdict and expands a failing command', async ({page}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await round(page, 3).click();
  await expect(page.locator('.sticky')).toContainText('Rejected');
  await expect(page.locator('.sticky')).toContainText('Reverted');
  const failing = page.getByRole('button', {name: /cargo test --release.*exit 1/}).first();
  await failing.click();
  await expect(failing).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('.out .fl').first()).toBeVisible();
});

test('a steer is queued on acknowledgment and applied when consumed', async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  const steer = page.getByRole('textbox', {name: 'Steer the next agent call'});
  await steer.fill('Measure lock hold time first.');
  await steer.press('Enter');
  await expect(page.getByText('Queued for the next agent call')).toBeVisible();
  await expect(steer).toHaveValue('');
  gateway.push({
    type: 'control',
    status: 'consumed',
    text: '/steer',
    agent_kind: 'judge',
    round_label: 'round-6-retry-1-judge',
  });
  await expect(page.getByText('Applied to the next agent call')).toBeVisible();
  await expect(page.getByText('Queued for the next agent call')).toHaveCount(0);
});

test('a finished run has no composer', async ({page}) => {
  await mockGateway(page, {through: FINISHED});
  await page.goto('/?token=e2e');
  await expect(page.locator('.titlebar')).toContainText('Completed');
  await expect(page.getByRole('textbox', {name: 'Steer the next agent call'})).toHaveCount(0);
});

test('the replay page runs without a gateway', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('.titlebar')).toContainText('Completed');
  await expect(round(page, 7)).toBeVisible();
});
