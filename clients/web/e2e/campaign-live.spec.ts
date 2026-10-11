import {expect, test, type Page} from '@playwright/test';

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

/** What a color-valued custom property resolves to in this browser, e.g.
 * `'rgb(138, 84, 0)'` for `--warning`. Lets a test compare an element's
 * computed color against the variable it is meant to render, independent of
 * whether the source uses hex, `rgb()`, or a named color. */
async function resolvedColorVariable(page: Page, variableName: string): Promise<string> {
  return page.evaluate(name => {
    const probe = document.createElement('span');
    probe.style.color = `var(${name})`;
    document.body.appendChild(probe);
    const resolved = getComputedStyle(probe).color;
    probe.remove();
    return resolved;
  }, variableName);
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

test('an active workstream shows its activity label in the table layout too', async ({page}) => {
  // Regression test: the table layout used to render an Attempt column but
  // drop the activity label entirely, even though the Kanban layout showed
  // both for the same live data.
  await page.goto('/?campaign-live&interval=20');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const workstreams = page.getByRole('region', {name: 'Workstreams'});
  await workstreams.getByRole('button', {name: 'Table'}).click();
  const table = workstreams.getByRole('table');

  // The replay script emits a scripted workstream-activity frame for the
  // first workstream partway through its run, well before the campaign
  // completes.
  await expect(table.locator('.activity-label').first()).toBeVisible({timeout: 40_000});
});

test('the Kanban attempt badge and activity label render their semantic colors', async ({page}) => {
  // Regression test: `.kanban-items span, .kanban-items small` (specificity
  // 0,1,1) used to outrank the single-class `.attempt-badge-warning` and
  // `.activity-mode-tool` rules (0,1,0) regardless of source order, so both
  // silently rendered as the container's default gray instead of the warning
  // amber / accent blue the Table layout already showed for the same data.
  await page.goto('/?campaign-live&interval=50');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const active = page.getByRole('region', {name: 'Active'});
  const warningBadge = active.locator('.attempt-badge-warning').first();
  const toolActivity = active.locator('.activity-mode-tool').first();

  // The replay script drives a repeated setup failure (warning badge) and a
  // concurrent tool-mode activity label for the same workstream.
  await expect(warningBadge).toBeVisible({timeout: 20_000});
  await expect(toolActivity).toBeVisible();

  const [badgeColor, activityColor, expectedWarning, expectedAccent] = await Promise.all([
    warningBadge.evaluate(el => getComputedStyle(el).color),
    toolActivity.evaluate(el => getComputedStyle(el).color),
    resolvedColorVariable(page, '--warning'),
    resolvedColorVariable(page, '--accent'),
  ]);

  expect(badgeColor).toBe(expectedWarning);
  expect(activityColor).toBe(expectedAccent);
});
