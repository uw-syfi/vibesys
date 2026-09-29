import {expect, type Page, test} from '@playwright/test';
import {FINISHED, MOCK_ANSWER, mockGateway, mockNotes, ROUND_6_FINISHED} from './gateway.js';

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

test('Notes opens from the ••• menu', async ({page}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitem', {name: 'Notes'}).click();
  await expect(page.getByRole('tab', {name: 'Notes'})).toHaveAttribute('aria-selected', 'true');
});

const askPane = async (page: Page) => {
  await page.getByRole('button', {name: 'Toggle side pane'}).click();
  const pane = page.getByRole('complementary', {name: 'Run details'});
  await pane.getByRole('tab', {name: 'Ask'}).click();
  return pane;
};

test('Ask: a question gets one answer; a model starts a thread; the switcher returns', async ({
  page,
}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  const pane = await askPane(page);
  const box = pane.getByRole('textbox', {name: 'Ask about this run'});
  await box.fill('Why did round 3 fail the judge?');
  await box.press('Enter');
  await expect(box).toHaveValue('');
  await expect(pane.locator('.human')).toHaveText(['Why did round 3 fail the judge?']);
  await expect(pane.locator('.answer')).toHaveCount(1);
  await expect(pane.locator('.answer')).toContainText(MOCK_ANSWER);
  await pane.getByRole('button', {name: 'Chat model'}).click();
  await page.getByRole('menuitemradio', {name: 'claude-sonnet-5'}).click();
  await expect(pane.getByRole('button', {name: 'Chat model'})).toContainText('claude-sonnet-5');
  await expect(pane.locator('.human')).toHaveCount(0);
  expect(
    gateway.requests
      .filter(request => request.type === 'query.chat_thread_create')
      .map(request => request.model),
  ).toEqual(['claude-sonnet-5']);
  await pane.getByRole('button', {name: /^Thread:/}).click();
  await expect(page.getByRole('menuitemradio')).toHaveCount(2);
  await page.getByRole('menuitemradio', {name: /^Why did round 3 fail the judge\?/}).click();
  await expect(pane.locator('.human')).toHaveCount(1);
});

test('Ask without a chat harness says so and offers no composer', async ({page}) => {
  await mockGateway(page, {chat: 'off'});
  await page.goto('/?token=e2e');
  const pane = await askPane(page);
  await expect(pane).toContainText('This run offers no chat harness.');
  await expect(pane.getByRole('textbox', {name: 'Ask about this run'})).toHaveCount(0);
});

test('Ask offers chat once a starting run reports its options', async ({page}) => {
  const gateway = await mockGateway(page, {chat: 'late'});
  await page.goto('/?token=e2e');
  const pane = await askPane(page);
  await expect(pane).toContainText('This run offers no chat harness.');
  gateway.setStatus('pausing');
  await expect(pane.getByRole('textbox', {name: 'Ask about this run'})).toBeVisible();
});

test('a failed options query says so; Retry asks again', async ({page}) => {
  const gateway = await mockGateway(page, {chat: 'error'});
  await page.goto('/?token=e2e');
  const pane = await askPane(page);
  await expect(pane).toContainText('Couldn’t check the chat harness.');
  await expect(pane).not.toContainText('This run offers no chat harness.');
  await pane.getByRole('button', {name: 'Retry'}).click();
  await expect(pane.getByRole('textbox', {name: 'Ask about this run'})).toBeVisible();
  expect(gateway.requests.filter(request => request.type === 'query.chat_options')).toHaveLength(2);
});

test('a failed options query is asked again when the connection returns', async ({page}) => {
  const gateway = await mockGateway(page, {chat: 'error'});
  await page.goto('/?token=e2e');
  const pane = await askPane(page);
  await expect(pane).toContainText('Couldn’t check the chat harness.');
  gateway.drop();
  await expect(pane.getByRole('textbox', {name: 'Ask about this run'})).toBeVisible();
  expect(gateway.requests.filter(request => request.type === 'query.chat_options')).toHaveLength(2);
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

test('a question that fails returns to the composer', async ({page}) => {
  await mockGateway(page, {reject: ['query.chat']});
  await page.goto('/?token=e2e');
  const pane = await askPane(page);
  const box = pane.getByRole('textbox', {name: 'Ask about this run'});
  await box.fill('Why did round 3 fail the judge?');
  await box.press('Enter');
  await expect(pane.getByRole('alert')).toContainText('Not answered');
  await expect(box).toHaveValue('Why did round 3 fail the judge?');
});

/** A run page the home server opened: only there does the page have a notes API. */
const HOME_RUN = '/?token=e2e&gateway=/';

const notesPane = async (page: Page) => {
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitem', {name: 'Notes'}).click();
  return page.getByRole('complementary', {name: 'Run details'});
};

test('Notes load, save as you type, and become a steer or ask draft without sending', async ({
  page,
}) => {
  const gateway = await mockGateway(page);
  const notes = await mockNotes(page, 'Check p99 before keeping round 7.');
  await page.goto(HOME_RUN);
  const pane = await notesPane(page);
  const editor = pane.getByRole('textbox', {name: 'Notes'});
  await expect(editor).toHaveValue('Check p99 before keeping round 7.');
  await editor.fill('Line one\nline two');
  await expect.poll(() => notes.puts.at(-1)).toBe('Line one\nline two');
  expect(notes.auth.every(value => value === 'Bearer e2e')).toBe(true);
  await pane.getByRole('button', {name: 'Use as steer draft'}).click();
  const steer = page.getByRole('textbox', {name: 'Steer the next agent call'});
  await expect(steer).toHaveValue('Line one line two');
  await expect(steer).toBeFocused();
  await pane.getByRole('button', {name: 'Use as ask draft'}).click();
  await expect(pane.getByRole('tab', {name: 'Ask'})).toHaveAttribute('aria-selected', 'true');
  await expect(pane.getByRole('textbox', {name: 'Ask about this run'})).toHaveValue(
    'Line one line two',
  );
  expect(
    gateway.requests.filter(
      request => request.type === 'command.steer' || request.type === 'query.chat',
    ),
  ).toEqual([]);
});

test('Notes: edits survive a tab switch and are saved', async ({page}) => {
  await mockGateway(page);
  const notes = await mockNotes(page, null);
  await page.goto(HOME_RUN);
  const pane = await notesPane(page);
  const editor = pane.getByRole('textbox', {name: 'Notes'});
  await editor.fill('Typed, then away at once');
  await pane.getByRole('tab', {name: 'Changes'}).click();
  await expect.poll(() => notes.puts.at(-1)).toBe('Typed, then away at once');
  await pane.getByRole('tab', {name: 'Notes'}).click();
  await expect(editor).toHaveValue('Typed, then away at once');
});

test('Notes: leaving the page saves what the timer has not', async ({page}) => {
  await mockGateway(page);
  const notes = await mockNotes(page, null);
  await page.clock.install();
  await page.goto(HOME_RUN);
  const pane = await notesPane(page);
  const editor = pane.getByRole('textbox', {name: 'Notes'});
  await expect(editor).toHaveValue('');
  // Timers stop here, so the 500 ms save never fires: only pagehide can send the text.
  await page.clock.pauseAt(Date.now() + 60_000);
  await editor.fill('Leaving');
  await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
  await expect.poll(() => notes.puts).toEqual(['Leaving']);
});

test('Notes: a run page the home server did not open says where notes live', async ({page}) => {
  await mockGateway(page);
  const notes = await mockNotes(page, 'Never loaded.');
  await page.goto('/?token=e2e');
  const pane = await notesPane(page);
  await expect(pane).toContainText('Notes are kept by the VibeSys home server');
  expect(notes.auth).toEqual([]);
});

test('Notes: a note that fails to load is not editable', async ({page}) => {
  await mockGateway(page);
  await page.route('**/api/notes/*', route => route.fulfill({status: 404, body: 'Not found'}));
  await page.goto(HOME_RUN);
  const pane = await notesPane(page);
  await expect(pane.getByRole('alert')).toHaveText('Couldn’t load notes. Retry');
  await expect(pane.getByRole('alert')).toHaveAttribute(
    'title',
    'Notes are unavailable (HTTP 404)',
  );
  await expect(pane.getByRole('textbox', {name: 'Notes'})).toHaveCount(0);
  await expect(pane.getByRole('button', {name: 'Retry'})).toBeVisible();
});

test('the ••• menu switches the theme and the choice survives a reload', async ({page}) => {
  await mockGateway(page);
  await page.emulateMedia({colorScheme: 'dark'});
  await page.goto('/?token=e2e');
  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.getByRole('button', {name: 'More'}).click();
  await expect(page.getByRole('menuitemradio', {name: 'System'})).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await page.getByRole('menuitemradio', {name: 'Light'}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  expect(await background()).toBe('rgb(252, 252, 253)');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitemradio', {name: 'System'}).click();
  await expect(page.locator('html')).not.toHaveAttribute('data-theme', /.+/);
  expect(await background()).toBe('rgb(17, 17, 19)');
});

test("the palette reaches Ask's model menu, a new thread and the theme", async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.locator('.titlebar').click();
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.type('chat model');
  await expect(page.getByRole('option', {name: /Chat model…/})).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('tab', {name: 'Ask'})).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('menu', {name: 'Chat model'})).toBeVisible();
  await page.keyboard.press('Escape');
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.type('new thread');
  await page.keyboard.press('Enter');
  await expect
    .poll(
      () => gateway.requests.filter(request => request.type === 'query.chat_thread_create').length,
    )
    .toBe(1);
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.type('theme: dark');
  await page.keyboard.press('Enter');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});
