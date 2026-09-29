import {expect, type Page, test} from '@playwright/test';
import {FINISHED, mockGateway, ROUND_6_FINISHED} from './gateway.js';

const rounds = (page: Page) => page.getByRole('navigation', {name: 'Runs'});
const round = (page: Page, n: number) =>
  rounds(page).getByRole('button', {name: new RegExp(`^Round ${n},`)});

test('the live round: the acting judge, the kept checkpoint, the sidebar rounds', async ({
  page,
}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await expect(round(page, 6)).toHaveAttribute('aria-current', 'true');
  // The selected row keeps its grid (a setup `.sel` wrapper once reset it to block).
  await expect(round(page, 6)).toHaveCSS('display', 'grid');
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

test('pause waits for the current call, then offers resume', async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  const title = page.locator('.titlebar');
  await title.getByRole('button', {name: 'Pause'}).click();
  await expect(title).toContainText('Pausing after the current call…');
  await expect(title.getByRole('button', {name: 'Pause'})).toHaveCount(0);
  // The judge's call finishes, and the round with it; then the pause takes effect.
  gateway.advance(ROUND_6_FINISHED);
  gateway.setStatus('paused');
  await expect(title).toContainText('Paused after round 6');
  await expect(page.locator('main .endline')).toHaveText('Round 7 starts when you resume.');
  await expect(page.locator('main .turn .spin')).toHaveCount(0);
  await expect(round(page, 6).locator('[aria-label="paused"]')).toHaveCount(0);
  await title.getByRole('button', {name: 'Resume'}).click();
  await expect(title).not.toContainText('Paused');
  expect(gateway.requests.map(request => request.type)).toEqual(
    expect.arrayContaining(['command.pause', 'command.resume']),
  );
});

test('a refused pause or resume clears its pending state and offers the control again', async ({
  page,
}) => {
  await mockGateway(page, {reject: ['command.pause']});
  await page.goto('/?token=e2e');
  const title = page.locator('.titlebar');
  await title.getByRole('button', {name: 'Pause'}).click();
  await expect(title).toContainText('Pause failed: The run refused.');
  await expect(title).toContainText('Judging round 6');
  await expect(title).not.toContainText('Pausing');
  await expect(title.getByRole('button', {name: 'Pause'})).toBeEnabled();

  const paused = await page.context().newPage();
  await mockGateway(paused, {
    through: ROUND_6_FINISHED,
    status: 'paused',
    reject: ['command.resume'],
  });
  await paused.goto('/?token=e2e');
  const pausedTitle = paused.locator('.titlebar');
  await pausedTitle.getByRole('button', {name: 'Resume'}).click();
  await expect(pausedTitle).toContainText('Resume failed: The run refused.');
  await expect(pausedTitle).toContainText('Paused after round 6');
  await expect(pausedTitle).not.toContainText('Resuming');
  await expect(pausedTitle.getByRole('button', {name: 'Resume'})).toBeEnabled();
});

test('stop asks first, names who finishes, and focuses Cancel', async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitem', {name: 'Stop run…'}).click();
  const dialog = page.getByRole('alertdialog', {name: 'Stop this run?'});
  await expect(dialog).toContainText('The judge finishes its call');
  await expect(dialog.getByRole('button', {name: 'Cancel'})).toBeFocused();
  // ⌘K leaves the open confirmation alone.
  await page.keyboard.press('ControlOrMeta+k');
  await expect(dialog).toBeVisible();
  await expect(page.getByRole('dialog', {name: 'Search and commands'})).toHaveCount(0);
  await dialog.getByRole('button', {name: 'Stop run'}).click();
  await expect(page.locator('.titlebar')).toContainText('Stopping after the current call…');
  expect(gateway.requests.some(request => request.type === 'command.stop')).toBe(true);
});

test('the pane opens beside the transcript; at 1024 the sidebar yields to it', async ({page}) => {
  await mockGateway(page);
  await page.setViewportSize({width: 1440, height: 900});
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: 'Toggle side pane'}).click();
  await expect(page.getByRole('tab', {name: 'Changes'})).toHaveAttribute('aria-selected', 'true');
  await expect(rounds(page)).toBeVisible();
  await page.setViewportSize({width: 1024, height: 900});
  await expect(rounds(page)).toHaveCount(0);
  expect((await page.locator('main').boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(560);
  await page.getByRole('button', {name: 'Show sidebar'}).click();
  await expect(rounds(page)).toBeVisible();
  await expect(page.getByRole('complementary', {name: 'Run details'})).toHaveCount(0);
});

test('the sidebar hides and returns, and resizes from the keyboard', async ({page}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  const edge = page.getByRole('separator', {name: 'Resize the sidebar'});
  await edge.focus();
  await page.keyboard.press('ArrowRight');
  await expect(edge).toHaveAttribute('aria-valuenow', '292');
  await page.getByRole('button', {name: 'Hide sidebar'}).click();
  await expect(rounds(page)).toHaveCount(0);
  await page.getByRole('button', {name: 'Show sidebar'}).click();
  await expect(rounds(page)).toBeVisible();
});

test('Ask and Notes say they are not available yet; Notes is also in the ••• menu', async ({
  page,
}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitem', {name: 'Notes'}).click();
  const pane = page.getByRole('complementary', {name: 'Run details'});
  await expect(pane).toContainText('Run notes are not available yet.');
  await pane.getByRole('tab', {name: 'Ask'}).click();
  await expect(pane).toContainText('Chat about this run is not available yet.');
});

test('changes: a patch, one the repository cannot produce, a truncated one, a running round', async ({
  page,
}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await round(page, 4).click();
  await page.getByRole('button', {name: 'Toggle side pane'}).click();
  const pane = page.getByRole('complementary', {name: 'Run details'});
  await expect(pane).toContainText('against the round 2 checkpoint');
  await expect(
    pane.getByRole('region', {name: 'src/batch.rs'}).locator('.diff .add').first(),
  ).toBeVisible();
  await expect(pane.getByRole('region', {name: 'src/kv_cache.rs'})).toContainText(
    'could not produce this patch',
  );
  await round(page, 3).click();
  const more = pane.getByRole('button', {name: /^\d+ more changed lines$/});
  await expect(more).toHaveAttribute('aria-expanded', 'false');
  await more.click();
  await expect(pane).toContainText("Patch truncated at the server's size bound.");
  await expect(pane.getByRole('button', {name: 'Copy command'})).toBeVisible();
  await round(page, 6).click();
  await expect(pane).toContainText('Changes appear when round 6 finishes.');
});

test('agents: one card per execution, top to bottom; a card filters the transcript', async ({
  page,
}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: 'Toggle side pane'}).click();
  await page.getByRole('tab', {name: 'Agents'}).click();
  const pane = page.getByRole('complementary', {name: 'Run details'});
  const cards = pane.locator('.ag');
  await expect(cards).toHaveCount(4);
  const ys = await cards.evaluateAll(elements =>
    elements.map(element => element.getBoundingClientRect().y),
  );
  expect([...ys].sort((left, right) => left - right)).toEqual(ys);
  await expect(pane).toContainText('4 invocations, order inferred');
  // Read-only graph: the cards' buttons are its only tab stops; wrappers and edges take no focus.
  await expect(pane.locator('.react-flow__edge')).toHaveCount(3);
  await expect(pane.locator('.react-flow [tabindex]:not([tabindex="-1"])')).toHaveCount(0);
  await pane.getByRole('button', {name: 'Close pane'}).focus();
  for (let index = 0; index < 4; index++) {
    await page.keyboard.press('Tab');
    await expect(cards.nth(index)).toBeFocused();
  }
  await page.keyboard.press('Tab');
  await expect(pane.locator('.detail button').first()).toBeFocused();
  await pane.getByRole('button', {name: /^Implementer/}).click();
  await expect(page.locator('.filterbar')).toContainText('Showing only Implementer (attempt 1)');
  await expect(page.locator('main .turn')).toHaveCount(1);
  await page.locator('.filterbar').getByRole('button', {name: 'Show all'}).click();
  await expect(page.locator('main .turn')).toHaveCount(4);
});

test('experiments: chart with legend, evidence per round, design summary', async ({page}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: 'Toggle side pane'}).click();
  await page.getByRole('tab', {name: 'Experiments'}).click();
  const pane = page.getByRole('complementary', {name: 'Run details'});
  await expect(
    pane.getByRole('img', {name: 'Retained metric and attempts by round'}),
  ).toBeVisible();
  await expect(pane.locator('.legend')).toContainText('Being judged');
  await pane.locator('.xrow', {hasText: 'Skip the post-sampling device sync'}).click();
  await expect(pane.locator('.ev')).toContainText('Pass criteria');
  await pane.getByRole('button', {name: 'View changes'}).click();
  await expect(page.getByRole('tab', {name: 'Changes'})).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.sticky')).toContainText('Round 3');
  await page.getByRole('tab', {name: 'Experiments'}).click();
  await pane.getByRole('button', {name: 'Design'}).click();
  await expect(pane).toContainText('src/sampler.rs');
});

test('⌘K opens the palette; a command runs and closes it; Escape closes it', async ({page}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.locator('.titlebar').click();
  await page.keyboard.press('ControlOrMeta+k');
  const palette = page.getByRole('dialog', {name: 'Search and commands'});
  await expect(palette).toBeVisible();
  await page.keyboard.type('round 3');
  await page.keyboard.press('Enter');
  await expect(palette).toHaveCount(0);
  await expect(page.locator('.sticky')).toContainText('Round 3');
  await page.getByRole('button', {name: /Search and commands/}).click();
  await page.keyboard.type('experiments');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('tab', {name: 'Experiments'})).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.press('Escape');
  await expect(palette).toHaveCount(0);
});

test('closing the palette returns focus to what opened it', async ({page}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  const steer = page.getByRole('textbox', {name: 'Steer the next agent call'});
  await steer.focus();
  await page.keyboard.press('ControlOrMeta+k');
  const palette = page.getByRole('dialog', {name: 'Search and commands'});
  await expect(palette.getByRole('combobox')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(palette).toHaveCount(0);
  await expect(steer).toBeFocused();
  const opener = page.getByRole('button', {name: /Search and commands/});
  await opener.click();
  await expect(palette).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(opener).toBeFocused();
});

test('a long palette list fades at its end and says it scrolls', async ({page}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: /Search and commands/}).click();
  const palette = page.getByRole('dialog', {name: 'Search and commands'});
  await expect(palette.locator('.list')).toHaveClass(/\bmore\b/);
  await expect(palette.locator('.count')).toContainText(', scroll for more');
  await page.keyboard.type('round 3');
  await expect(palette.locator('.count')).not.toContainText('scroll for more');
});

test('the agent filter ends with its round: the live round advancing, or another round picked', async ({
  page,
}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: 'Toggle side pane'}).click();
  await page.getByRole('tab', {name: 'Agents'}).click();
  const pane = page.getByRole('complementary', {name: 'Run details'});
  await pane.getByRole('button', {name: /^Implementer/}).click();
  await expect(page.locator('.filterbar')).toBeVisible();
  // Round 7 starts: the followed transcript shows it whole.
  gateway.advance(ROUND_6_FINISHED + 4);
  await expect(page.locator('.sticky')).toContainText('Round 7');
  await expect(page.locator('.filterbar')).toHaveCount(0);
  await expect(page.locator('main .turn').first()).toBeVisible();
  await round(page, 6).click();
  await expect(page.locator('.filterbar')).toHaveCount(0);
  await expect(page.locator('main .turn')).toHaveCount(4);
  await pane.getByRole('button', {name: /^Implementer/}).click();
  await expect(page.locator('.filterbar')).toBeVisible();
  await round(page, 3).click();
  await expect(page.locator('.filterbar')).toHaveCount(0);
});

test('a failed run says why in the title row, the full diagnostic on hover', async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  gateway.push({
    type: 'run_failed',
    text: 'Benchmark harness crashed',
    diagnostic: {
      code: 'benchmark_crashed',
      summary: 'Benchmark harness crashed',
      detail: 'cargo bench exited with status 137 after 41.7s (killed by the OOM killer).',
      scope: 'run',
      severity: 'fatal',
    },
  });
  const status = page.locator('.titlebar .status');
  await expect(status).toHaveText('Failed: Benchmark harness crashed');
  await expect(status).toHaveAttribute(
    'title',
    'cargo bench exited with status 137 after 41.7s (killed by the OOM killer).',
  );
  await expect(page.locator('.titlebar').getByRole('button', {name: 'Pause'})).toHaveCount(0);
});
