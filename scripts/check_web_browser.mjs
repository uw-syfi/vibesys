// Exercise the built UI through Vite, the gateway, and a real deterministic ServerRuntime.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {createServer} from 'node:net';
import {join, resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';

const root = fileURLToPath(new URL('../', import.meta.url));
const directory = await mkdtemp('/tmp/vs-web-');
const socket = join(directory, 'control.sock');
const children = [];
let browser;

async function freePort() {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

function launch(command, args) {
  const child = spawn(command, args, {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
  let output = '';
  child.stdout.on('data', data => {
    output += data;
  });
  child.stderr.on('data', data => {
    output += data;
  });
  children.push({child, output: () => output});
  return child;
}

async function until(check, description) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await check()) return;
    await delay(100);
  }
  throw new Error(`Timed out: ${description}`);
}

/** The run id of every `subscribed` message the page receives, in order. */
function watchRunIds(page) {
  const runIds = [];
  page.on('websocket', ws =>
    ws.on('framereceived', ({payload}) => {
      if (typeof payload !== 'string' || !payload.includes('"subscribed"')) return;
      const message = JSON.parse(payload);
      if (message.type === 'subscribed') runIds.push(message.run_id);
    }),
  );
  return runIds;
}

const rounds = page => page.getByRole('navigation', {name: 'Rounds'});
const round = (page, number) =>
  rounds(page).getByRole('button', {name: new RegExp(`^R${number}\\b`)});

async function checkRoundSelection(browser, origin) {
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  let runId = 'selection-run';
  let socket;
  const subscriptions = [];
  const output = (sequence, number, content) => ({
    sequence,
    type: 'agent_output_chunk',
    timestamp: '2026-09-21T12:00:00Z',
    round_label: `round-${number}`,
    agent_kind: 'implementer',
    data: {kind: 'agent_output_chunk', content, channel: 'assistant'},
  });
  const finished = (sequence, number) => ({
    sequence,
    type: 'round_finished',
    round_label: `round-${number}`,
    timestamp: '2026-09-21T12:00:01Z',
    data: {kind: 'round_finished', attempts: 1, judge_verdict: 'pass'},
  });
  await page.route('**/api/request', async route => {
    const request = route.request().postDataJSON();
    const fields =
      request.type === 'query.snapshot'
        ? {snapshot: {run_id: runId, sequence: 0, status: 'running'}}
        : request.type === 'query.experiments'
          ? {experiments_ready: true, experiments: []}
          : {};
    await route.fulfill({
      json: {protocol_version: 1, request_id: request.request_id, ok: true, ...fields},
    });
  });
  await page.routeWebSocket('**/api/events', ws => {
    socket = ws;
    ws.onMessage(raw => {
      const request = JSON.parse(raw);
      subscriptions.push(request.after_sequence);
      const selection = runId === 'selection-run';
      ws.send(
        JSON.stringify({
          type: 'subscribed',
          request_id: request.request_id,
          run_id: runId,
          latest_sequence: selection ? 100 : 3,
        }),
      );
      const events = selection
        ? [
            output(1, 1, 'Round one evidence'),
            finished(2, 1),
            output(3, 2, 'Round two evidence'),
            finished(4, 2),
            output(100, 3, 'Round three evidence'),
          ]
        : request.after_sequence === 0
          ? [output(1, 1, 'Replacement round one'), output(3, 2, 'Replacement round two')]
          : [];
      ws.send(
        JSON.stringify({
          type: 'event_batch',
          events,
          through_sequence: selection ? 100 : 3,
          history_after_sequence: 0,
          active_executions: [],
        }),
      );
    });
  });
  try {
    await page.goto(origin);
    const log = page.getByRole('log');
    // The latest round is selected by default.
    await log.getByText('Round three evidence', {exact: true}).waitFor();
    assert.equal(await log.getByText('Round one evidence', {exact: true}).count(), 0);
    await round(page, 1).click();
    await log.getByText('Round one evidence', {exact: true}).waitFor();
    assert.equal(await log.getByText('Round two evidence', {exact: true}).count(), 0);
    assert.equal(await log.getByText('Round three evidence', {exact: true}).count(), 0);
    await round(page, 2).click();
    await log.getByText('Round two evidence', {exact: true}).waitFor();
    assert.equal(await log.getByText('Round one evidence', {exact: true}).count(), 0);

    // A replaced run drops the old pick and follows the new run's latest round.
    runId = 'replacement-run';
    socket.close();
    await log.getByText('Replacement round two', {exact: true}).waitFor();
    assert.equal(await log.getByText('Replacement round one', {exact: true}).count(), 0);
    assert.equal(await log.getByText('Round two evidence', {exact: true}).count(), 0);
    assert.equal(await round(page, 3).count(), 0);
    assert.deepEqual(subscriptions, [0, 100, 0]);
  } finally {
    await page.close();
  }
}

/**
 * Live follow, focus, tooltip, drawer, and composer interactions on a mocked two-round run:
 * round 1 finished, round 2 live with a collapsed orchestrator group above the implementer.
 */
async function checkInteractions(browser, origin) {
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.setDefaultTimeout(10_000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let sequence = 0;
  const event = (type, number, kind, data) => ({
    sequence: ++sequence,
    type,
    timestamp: '2026-09-21T12:00:00Z',
    round_label: `round-${number}`,
    agent_kind: kind,
    data,
  });
  const say = (number, kind, content) =>
    event('agent_output_chunk', number, kind, {
      kind: 'agent_output_chunk',
      content,
      channel: 'assistant',
    });
  const lines = (number, kind, prefix, count) =>
    Array.from({length: count}, (_, index) => say(number, kind, `${prefix} ${index + 1}`));
  const events = [
    say(1, 'implementer', 'Round one evidence'),
    event('round_finished', 1, null, {kind: 'round_finished', attempts: 1, judge_verdict: 'pass'}),
    ...lines(2, 'orchestrator', 'Plan line', 40),
    say(2, 'implementer', 'Implementer started'),
  ];
  const implementer = {
    execution_id: 'implementer-2',
    agent_kind: 'implementer',
    round_label: 'round-2',
    stage: 'implementer',
    attempt: 1,
    assignment: '',
    started_at: '2026-09-21T12:00:00Z',
    activity: {mode: 'text', summary: ''},
  };
  // A file change kind this build does not know.
  const design = [
    {round: 1, commit: 'a9cf40c4fbdf', files: [{path: 'src/copy.py', change: 'copied'}]},
  ];
  const steers = [];
  let hold = null;
  let socket;
  await page.route('**/api/request', async route => {
    const request = route.request().postDataJSON();
    if (request.type === 'command.steer') {
      steers.push(request.text);
      await hold;
    }
    const fields =
      request.type === 'query.snapshot'
        ? {snapshot: {run_id: 'interaction-run', sequence, status: 'running'}}
        : request.type === 'query.experiments'
          ? {experiments_ready: true, experiments: []}
          : request.type === 'query.design'
            ? {design_ready: true, design}
            : request.type.startsWith('command.')
              ? {ack: {action: request.type.slice('command.'.length), status: 'pending'}}
              : {};
    await route.fulfill({
      json: {protocol_version: 1, request_id: request.request_id, ok: true, ...fields},
    });
  });
  const batch = (batchEvents, active) =>
    JSON.stringify({
      type: 'event_batch',
      events: batchEvents,
      through_sequence: sequence,
      history_after_sequence: 0,
      active_executions: active,
    });
  await page.routeWebSocket('**/api/events', ws => {
    socket = ws;
    ws.onMessage(raw => {
      const request = JSON.parse(raw);
      ws.send(
        JSON.stringify({
          type: 'subscribed',
          request_id: request.request_id,
          run_id: 'interaction-run',
          latest_sequence: sequence,
        }),
      );
      ws.send(batch(events, [implementer]));
    });
  });
  const push = (batchEvents, active = [implementer]) => socket.send(batch(batchEvents, active));
  const log = page.locator('#log');
  const pinned = () =>
    log.evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight <= 1);
  const jump = page.getByRole('button', {name: 'Jump to latest'});
  const tip = (text, scope = page) => scope.locator('#tip', {hasText: text});
  const drawer = page.locator('dialog.insp-drawer');
  try {
    await page.goto(origin);
    await log.getByText('Implementer started', {exact: true}).waitFor();

    // Opening a fold while following leaves the live edge.
    await log.locator('details.fold > summary').first().click();
    await jump.waitFor();
    assert.equal(await pinned(), false, 'an opened fold keeps its place');
    // Back at the live edge, appends keep the newest line in view.
    await jump.click();
    for (let batchIndex = 1; batchIndex <= 3; batchIndex++) {
      push(lines(2, 'implementer', `Live batch ${batchIndex} line`, 10));
      await log.getByText(`Live batch ${batchIndex} line 10`, {exact: true}).waitFor();
      await delay(100);
      assert.equal(await pinned(), true, `following after append ${batchIndex}`);
    }
    assert.equal(await jump.count(), 0, 'no Jump to latest while following');

    // Focus inside the log carries across the re-key a new round causes.
    await log.focus();
    push(
      [
        event('round_finished', 2, null, {
          kind: 'round_finished',
          attempts: 1,
          judge_verdict: 'pass',
        }),
        event('phase_started', '3-pre', 'orchestrator', {
          kind: 'phase',
          phase: 'orchestrator',
          attempt: null,
        }),
      ],
      [],
    );
    await page.getByRole('log', {name: 'Round 3 log'}).waitFor();
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'log', 'log keeps focus');

    // A j/k selection change carries rail focus to the new row.
    await rounds(page).locator('[aria-current="true"]').focus();
    await page.keyboard.press('k');
    await round(page, 2).and(page.locator('[aria-current="true"]')).waitFor();
    assert.equal(
      await round(page, 2).evaluate(node => node === document.activeElement),
      true,
      'rail focus follows the selection',
    );

    // A keyboard-focused tip stays while the pointer moves elsewhere.
    const pause = page.getByRole('button', {name: 'Pause', exact: true});
    await pause.focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await tip('Pause after the current agent call').waitFor();
    await page.mouse.move(700, 600);
    await page.mouse.move(720, 640);
    await delay(300);
    assert.equal(await tip('Pause after').isVisible(), true, 'the focused tip stays pinned');

    // Drawer (768-1023 px): an unknown change kind renders, and Esc on a tip hides only the tip.
    await page.setViewportSize({width: 900, height: 1000});
    await round(page, 1).click();
    await drawer.waitFor();
    assert.equal(
      await drawer
        .locator('.file')
        .innerText()
        .then(text => text.replace(/\s+/g, ' ').trim()),
      'Changed src/copy.py',
    );
    await drawer.locator('.file .ic').hover();
    await tip('Changed', drawer).waitFor();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#tip').isHidden(), true, 'Esc hides the tip');
    assert.equal(await drawer.evaluate(node => node.open), true, 'Esc on a tip keeps the dialog');
    // Crossing 1024 px with the drawer open, and back, reopens it.
    await page.setViewportSize({width: 1440, height: 1000});
    await page.locator('aside.insp').waitFor();
    await page.setViewportSize({width: 900, height: 1000});
    await drawer.waitFor();
    assert.equal(await drawer.evaluate(node => node.open), true, 'the drawer reopens');
    await page.mouse.move(5, 995);
    await delay(300);
    await page.keyboard.press('Escape');
    await drawer.waitFor({state: 'hidden'});

    // Enter during IME composition, including Safari's keyCode 229, does not send.
    await page.setViewportSize({width: 1440, height: 1000});
    const steer = page.getByLabel('Steer the run');
    await steer.fill('kanji');
    for (const init of [{isComposing: true}, {keyCode: 229}]) {
      await steer.dispatchEvent('keydown', {
        key: 'Enter',
        bubbles: true,
        cancelable: true,
        ...init,
      });
    }
    await delay(150);
    assert.deepEqual(steers, [], 'an IME Enter sent the steer');
    // Text typed while a send is in flight stays.
    let release;
    hold = new Promise(resolve => {
      release = resolve;
    });
    await steer.fill('first');
    await steer.press('Enter');
    await until(async () => steers.length === 1, 'steer sent');
    await steer.press('End');
    await steer.pressSequentially(' and more');
    release();
    await delay(300);
    assert.deepEqual(steers, ['first']);
    assert.equal(await steer.inputValue(), 'first and more', 'typed text kept');
    assert.deepEqual(errors, []);
  } finally {
    await page.close();
  }
}

try {
  const gatewayPort = await freePort();
  const uiPort = await freePort();
  const origin = `http://127.0.0.1:${uiPort}`;
  launch('uv', ['run', 'python', '-m', 'tests.server.browser_fixture', '--control-socket', socket]);
  launch('uv', [
    'run',
    'python',
    '-m',
    'entrypoints.browser_gateway',
    '--control-socket',
    socket,
    '--port',
    String(gatewayPort),
    '--dev-origin',
    origin,
  ]);
  const vite = spawn('pnpm', ['--filter', '@vibesys/web', 'preview', '--port', String(uiPort)], {
    cwd: root,
    env: {...process.env, VIBESYS_GATEWAY_URL: `http://127.0.0.1:${gatewayPort}`},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let viteOutput = '';
  vite.stdout.on('data', data => {
    viteOutput += data;
  });
  vite.stderr.on('data', data => {
    viteOutput += data;
  });
  children.push({child: vite, output: () => viteOutput});
  await until(async () => {
    try {
      return (await fetch(origin)).ok;
    } catch {
      return false;
    }
  }, 'Vite preview ready');
  browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.setDefaultTimeout(10_000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const runIds = watchRunIds(page);
  await page.goto(origin);
  await page
    .getByText(/Browser fixture step/)
    .first()
    .waitFor();
  const runId = runIds.at(-1);
  assert.ok(runId, 'the page subscribed to a run');
  const pause = page.getByRole('button', {name: 'Pause', exact: true});
  await until(async () => (await pause.getAttribute('aria-disabled')) !== 'true', 'Pause enabled');
  await pause.click();
  const resume = page.getByRole('button', {name: 'Resume', exact: true});
  await resume.waitFor();
  await page.getByLabel('Steer the run').fill('Preserve deterministic evidence');
  await page.getByRole('button', {name: 'Send', exact: true}).click();
  await page.locator('.qline').filter({hasText: 'Preserve deterministic evidence'}).waitFor();
  await resume.click();
  await page
    .getByText(/Browser fixture step.*Preserve deterministic evidence/s)
    .first()
    .waitFor();
  await page.reload();
  await page
    .getByText(/Preserve deterministic evidence/)
    .first()
    .waitFor();
  assert.equal(runIds.at(-1), runId, 'refresh preserves run');
  await page.getByLabel('Steer the run').fill('finish-browser-fixture');
  await page.getByRole('button', {name: 'Send', exact: true}).click();
  // The run's own outcome, in the header: the agent graph says "Completed" of each agent too.
  const completed = page.getByRole('banner').getByText('Completed', {exact: true});
  await completed.waitFor();
  // A finished ServerRuntime waits for its last subscriber. Closing every tab
  // must leave the gateway's subscription holding it for later inspection.
  await page.goto('about:blank');
  await delay(300);
  await page.goto(origin);
  await completed.waitFor();
  assert.equal(runIds.at(-1), runId, 'gateway retains a completed run without tabs');
  await page.setViewportSize({width: 390, height: 844});
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    true,
    'mobile overflow',
  );
  assert.equal(await rounds(page).isVisible(), true, 'the phone keeps the round strip');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement !== document.body), true);
  assert.deepEqual(errors, []);
  const artifacts = process.env['VIBESYS_WEB_SCREENSHOT'];
  if (artifacts) await page.screenshot({path: resolve(artifacts), fullPage: true});
  await checkRoundSelection(browser, origin);
  await checkInteractions(browser, origin);
  console.log(
    'Browser smoke passed: live events, pause/resume/steer, replay after refresh, completion, mobile layout, keyboard focus, round selection, run replacement, live follow, focus carry, tooltips, drawer, composer.',
  );
} catch (error) {
  for (const entry of children) console.error(entry.output());
  throw error;
} finally {
  await browser?.close();
  for (const {child} of children.toReversed()) {
    if (child.exitCode !== null) continue;
    child.kill('SIGTERM');
    await Promise.race([new Promise(resolve => child.once('exit', resolve)), delay(2000)]);
    if (child.exitCode === null) child.kill('SIGKILL');
  }
  await rm(directory, {recursive: true, force: true});
}
