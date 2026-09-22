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

async function checkHypothesisSelection(browser, origin) {
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  let runId = 'selection-run';
  let active = true;
  let socket;
  const subscriptions = [];
  const output = (sequence, round, content) => ({
    sequence,
    type: 'agent_output_chunk',
    timestamp: '2026-09-21T12:00:00Z',
    round_label: `Round ${round}`,
    data: {kind: 'agent_output_chunk', content, channel: 'assistant'},
  });
  await page.route('**/api/request', async route => {
    const request = route.request().postDataJSON();
    const fields = request.type === 'query.snapshot'
      ? {snapshot: {run_id: runId, sequence: 0, status: 'running'}}
      : request.type === 'query.experiments'
        ? {experiments_ready: true, experiments: [
            {hypothesis_id: 'previous', title: 'Previous hypothesis', first_round: 0, last_round: 0, active: false},
            {hypothesis_id: 'h1', title: 'Continuation hypothesis', first_round: 1, last_round: 1, active},
          ]}
        : {};
    await route.fulfill({json: {request_id: request.request_id, ok: true, ...fields}});
  });
  await page.routeWebSocket('**/api/events', ws => {
    socket = ws;
    ws.onMessage(raw => {
      const request = JSON.parse(raw);
      subscriptions.push(request.after_sequence);
      ws.send(JSON.stringify({type: 'subscribed', request_id: request.request_id, run_id: runId, latest_sequence: runId === 'selection-run' ? 100 : 3}));
      const events = runId === 'selection-run'
        ? [output(1, 0, 'Previous round evidence'), output(2, 1, 'Completed hypothesis evidence'), output(3, 2, 'Live continuation evidence'), output(4, 3, 'Unrelated completed evidence'), {sequence: 100, type: 'round_finished', round_label: 'Round 3', timestamp: '2026-09-21T12:00:01Z'}]
        : request.after_sequence === 0 ? [output(1, 1, 'Replacement round one'), output(3, 2, 'Replacement round two')] : [];
      ws.send(JSON.stringify({type: 'event_batch', events, through_sequence: runId === 'selection-run' ? 100 : 3, history_after_sequence: 0, active_executions: []}));
    });
  });
  try {
    await page.goto(origin);
    const transcript = page.getByRole('region', {name: 'Run activity transcript'});
    await page.getByRole('button', {name: /Continuation hypothesis/}).click();
    await transcript.getByText('Live continuation evidence', {exact: true}).waitFor();
    assert.equal(await transcript.getByText('Completed hypothesis evidence', {exact: true}).count(), 1);
    assert.equal(await transcript.getByText('Previous round evidence', {exact: true}).count(), 0);
    assert.equal(await transcript.getByText('Unrelated completed evidence', {exact: true}).count(), 0);
    assert.equal(await page.getByRole('button', {name: /Round 3/}).count(), 0);
    await page.getByRole('button', {name: /Round 2/}).click();
    await transcript.getByText('Live continuation evidence', {exact: true}).waitFor();
    assert.equal(await transcript.getByText('Completed hypothesis evidence', {exact: true}).count(), 0);
    await page.getByRole('button', {name: /Previous hypothesis/}).click();
    assert.equal(await page.getByRole('button', {name: /Round 2/}).count(), 0);
    assert.equal(await transcript.getByText('Live continuation evidence', {exact: true}).count(), 0);

    active = false;
    await page.getByRole('button', {name: 'Refresh data'}).click();
    await page.getByRole('button', {name: /Continuation hypothesis.*Under investigation/}).waitFor();
    await page.getByRole('button', {name: /Continuation hypothesis/}).click();
    assert.equal(await transcript.getByText('Completed hypothesis evidence', {exact: true}).count(), 1);
    assert.equal(await transcript.getByText('Live continuation evidence', {exact: true}).count(), 0);
    assert.equal(await page.getByRole('button', {name: /Round 2/}).count(), 0);
    await page.getByRole('button', {name: /Round 1/}).click();

    runId = 'replacement-run';
    socket.close();
    await until(async () => (await page.locator('.run-id').textContent()) === runId, 'replacement run identity');
    await transcript.getByText('Replacement round two', {exact: true}).waitFor();
    assert.equal(await transcript.getByText('Replacement round one', {exact: true}).count(), 1);
    assert.equal(await transcript.getByText('Completed hypothesis evidence', {exact: true}).count(), 0);
    assert.equal(await page.getByRole('button', {name: /All activity.*The complete run/}).getAttribute('aria-pressed'), 'true');
    assert.deepEqual(subscriptions, [0, 100, 0]);
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
  await page.goto(origin);
  await page.getByText('Live connection', {exact: true}).waitFor();
  await page
    .getByText(/Browser fixture step/)
    .first()
    .waitFor();
  const runId = await page.locator('.run-id').textContent();
  await page.getByRole('button', {name: 'Pause run'}).click();
  await until(
    () => page.getByRole('button', {name: 'Resume', exact: true}).isEnabled(),
    'pause boundary',
  );
  await page.getByLabel('Steer the run').fill('Preserve deterministic evidence');
  await page.getByRole('button', {name: 'Send guidance'}).click();
  await page.getByText('Guidance queued.', {exact: false}).waitFor();
  await page.getByRole('button', {name: 'Resume', exact: true}).click();
  await page
    .getByText(/Browser fixture step.*Preserve deterministic evidence/s)
    .first()
    .waitFor();
  await page.reload();
  await page.getByText('Live connection', {exact: true}).waitFor();
  await page
    .getByText(/Preserve deterministic evidence/)
    .first()
    .waitFor();
  assert.equal(await page.locator('.run-id').textContent(), runId, 'refresh preserves run');
  await page.getByLabel('Steer the run').fill('finish-browser-fixture');
  await page.getByRole('button', {name: 'Send guidance'}).click();
  await page.locator('.run-subtitle .badge').filter({hasText: 'completed'}).waitFor();
  // A finished ServerRuntime waits for its last subscriber. Closing every tab
  // must leave the gateway's subscription holding it for later inspection.
  await page.goto('about:blank');
  await delay(300);
  await page.goto(origin);
  await page.locator('.run-subtitle .badge').filter({hasText: 'completed'}).waitFor();
  assert.equal(
    await page.locator('.run-id').textContent(),
    runId,
    'gateway retains a completed run without tabs',
  );
  await page.getByRole('button', {name: 'Performance', exact: true}).click();
  await page.getByText(/performance data is not available yet/).waitFor();
  await page
    .getByRole('navigation', {name: 'Workspace views'})
    .getByRole('button', {name: 'Activity'})
    .click();
  await page.setViewportSize({width: 390, height: 844});
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    true,
    'mobile overflow',
  );
  await page.getByRole('button', {name: 'Hypotheses & rounds'}).click();
  assert.equal(await page.getByRole('complementary', {name: 'Run navigation'}).isVisible(), true);
  await page.getByRole('button', {name: 'Hypotheses & rounds'}).click();
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement !== document.body), true);
  assert.deepEqual(errors, []);
  const artifacts = process.env['VIBESYS_WEB_SCREENSHOT'];
  if (artifacts) await page.screenshot({path: resolve(artifacts), fullPage: true});
  await checkHypothesisSelection(browser, origin);
  console.log(
    'Browser smoke passed: live events, pause/resume/steer, replay after refresh, readiness, mobile layout, keyboard focus, hypothesis continuation selection, run replacement.',
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
