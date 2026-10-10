import {expect, test} from '@playwright/test';

/**
 * The replay-as-live route streams the campaign fixture over Server-Sent Events
 * and folds it into the same dashboard. These assertions prove the live
 * behavior the fixture route cannot: the record grows from empty, the run
 * status comes from the stream (not the cursor), and scrubbing pauses the
 * follow without rewriting that status.
 */

function recordedCount(text: string): number {
  return Number(/(\d+) recorded/.exec(text)?.[1] ?? '0');
}

test('streams the campaign in live and completes', async ({page}) => {
  await page.goto('/?campaign-live&interval=50');

  // The header appears as soon as the campaign-init frame folds in.
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const runState = page.locator('.run-state');
  const performance = page.getByRole('region', {name: 'Performance trajectory'});

  // While frames are still arriving the run reports itself active, and fewer
  // than all 111 measurements have been recorded.
  await expect(runState).toContainText('active');
  expect(recordedCount(await performance.innerText())).toBeLessThan(111);

  // The full campaign streams in and the terminal status frame flips the run.
  await expect(performance).toContainText('111 recorded', {timeout: 40_000});
  await expect(runState).toContainText('completed', {timeout: 40_000});
  await expect(page.getByText('2,242.4 tok/s', {exact: true})).toBeVisible();

  // Following the tail left the cursor at the end.
  await expect(page.getByText(/111\s*\/\s*111/)).toBeVisible();
});

test('scrubbing pauses the follow and keeps the folded run status', async ({page}) => {
  await page.goto('/?campaign-live&interval=20');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const performance = page.getByRole('region', {name: 'Performance trajectory'});
  await expect(performance).toContainText('111 recorded', {timeout: 40_000});

  const runState = page.locator('.run-state');
  await expect(runState).toContainText('completed', {timeout: 40_000});

  // Scrub back into the past. The cursor pins there (follow is paused)...
  const slider = page.getByRole('slider', {name: 'Campaign measurement'});
  await slider.fill('12');
  await expect(slider).toHaveValue('12');
  await expect(page.getByText(/13\s*\/\s*111/)).toBeVisible();

  // ...but the run status stays completed, because it comes from the folded
  // stream, not from the cursor position.
  await expect(runState).toContainText('completed');
});
