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

test('pause waits for the current call, then offers resume', async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  const title = page.locator('.titlebar');
  await title.getByRole('button', {name: 'Pause'}).click();
  await expect(title).toContainText('Pausing after the current call…');
  await expect(title.getByRole('button', {name: 'Pause'})).toHaveCount(0);
  gateway.setStatus('paused');
  await expect(title).toContainText('Paused in round 6');
  await title.getByRole('button', {name: 'Resume'}).click();
  await expect(title).toContainText('Judging round 6');
  expect(gateway.requests.map(request => request.type)).toEqual(
    expect.arrayContaining(['command.pause', 'command.resume']),
  );
});

test('stop asks first, names who finishes, and focuses Cancel', async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitem', {name: 'Stop run…'}).click();
  const dialog = page.getByRole('alertdialog', {name: 'Stop this run?'});
  await expect(dialog).toContainText('The judge finishes its call');
  await expect(dialog.getByRole('button', {name: 'Cancel'})).toBeFocused();
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
  await expect(pane).toContainText("Patch truncated at the server's size bound.");
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
  await expect(page.getByRole('tab', {name: 'Experiments'})).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.press('Escape');
  await expect(palette).toHaveCount(0);
});
