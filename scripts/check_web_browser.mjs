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
  const ran = (number, kind, command, stdout, description = 'Ran a command') => [
    event('tool_call', number, kind, {
      kind: 'tool_call',
      tool: 'Bash',
      call_id: command,
      args: {command, description},
    }),
    event('tool_result', number, kind, {
      kind: 'tool_result',
      tool: 'Bash',
      call_id: command,
      content: stdout,
      is_error: false,
      payload: {kind: 'command', stdout, stderr: '', exit_code: 0, duration: 0.4},
    }),
  ];
  const events = [
    say(1, 'implementer', 'Round one evidence'),
    event('round_finished', 1, null, {kind: 'round_finished', attempts: 1, judge_verdict: 'pass'}),
    ...lines(2, 'orchestrator', 'Plan line', 40),
    // Three adjacent calls of one verb fold into a run row, inside a group that is itself
    // folded because it is not the last: a closed fold inside a closed fold, which is the
    // shape the cursor has to step over.
    ...ran(2, 'orchestrator', 'grep one', 'one\n'),
    ...ran(2, 'orchestrator', 'grep two', 'two\n'),
    ...ran(2, 'orchestrator', 'grep three', 'three\n'),
    say(2, 'implementer', 'Implementer started'),
    // A line with no break in it, so the output's own horizontal scroll is exercised rather
    // than the column being widened by it.
    ...ran(2, 'implementer', 'make bench', `ops/sec 1420\nbench ok\n${'x'.repeat(400)}\n`),
    say(2, 'implementer', 'After the benchmark'),
    // A call with no result: the row is the live one, and its output section says so.
    {
      ...event('tool_call', 2, 'implementer', {
        kind: 'tool_call',
        tool: 'Bash',
        call_id: 'make slow',
        args: {command: 'make slow', description: 'Ran a command'},
      }),
      invocation_id: 'implementer-2',
    },
    event('usage_update', 2, 'implementer', {
      kind: 'usage_update',
      input_tokens: 12_400,
      context_window: 200_000,
      model: 'claude-opus-5',
    }),
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

    // A tool row's full output opens in the inspector, and no log row moves when it does. The
    // row measured sits *below* the one selected, where a reflow would actually show, and the
    // page must not gain a sideways scroll from the 400-character line in that output.
    const below = log.getByText('After the benchmark', {exact: true});
    const belowTop = () => below.evaluate(node => node.getBoundingClientRect().top);
    const wide = () => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
    const call = log.locator('.row[data-tool]:visible').first();
    await call.scrollIntoViewIfNeeded();
    const restingTop = await belowTop();
    await call.click();
    const out = page.locator('aside.insp .out');
    await out.waitFor();
    assert.match(await out.innerText(), /bench ok/, "the row's own output");
    assert.equal(
      await page.locator('aside.insp .out-head').innerText(),
      'make bench',
      'the section names the row it came from',
    );
    assert.equal(await belowTop(), restingTop, 'opening the output moved the row below it');
    assert.equal(await wide(), true, 'a long output line widened the page');
    // The header's context meter, from the run's own usage_update.
    await page.getByRole('banner').getByText('12k/200k context').waitFor();

    // The sticky role header covers the rows' own band, or a selected row passing under it
    // leaks its edge mark into the gutter beside the role name. The band itself is unchanged:
    // 20px rows on a 22px pitch.
    const band = await log.evaluate(node => {
      const group = [...node.querySelectorAll('.grp')].find(
        grp => grp.querySelector(':scope > .what > .row') !== null,
      );
      const what = group.querySelector(':scope > .what');
      const row = what.querySelector(':scope > .row').getBoundingClientRect();
      const who = group.querySelector('.who').getBoundingClientRect();
      // Row height plus the gap between rows is the pitch.
      const gap = Number.parseFloat(getComputedStyle(what).rowGap);
      return [who.left, row.left, who.right, row.right, row.height, row.height + gap];
    });
    assert.deepEqual(band.slice(0, 2), [band[1], band[1]], 'the header left the rows a gutter');
    assert.deepEqual(band.slice(2, 4), [band[3], band[3]], 'the header left the rows a gutter');
    assert.equal(band[4], 20, 'the row height moved');
    assert.equal(band[5], 22, 'the row pitch moved');

    // The live row carries both marks when it is also the selected one: the tint says which
    // call is running, the edge says which one the inspector is quoting.
    const running = log.locator('.row.now');
    await running.click();
    await page.getByText('Still running').waitFor();
    const marks = await running.evaluate(node => {
      const style = getComputedStyle(node);
      return [style.backgroundColor, style.boxShadow, node.className];
    });
    assert.equal(marks[2], 'row now sel', 'the live row is also the selected one');
    assert.notEqual(marks[0], 'rgba(0, 0, 0, 0)', 'the live tint survived the selection');
    assert.notEqual(marks[1], 'none', 'the selection marks the live row');
    await page.keyboard.press('Escape');
    await page.getByText('Still running').waitFor({state: 'detached'});

    // Arrows in the log move a row cursor that is real focus, so it is announced; the round
    // stays where it is, and so does every row.
    const cursor = () => page.evaluate(() => document.activeElement?.id ?? null);
    const inLog = () =>
      page.evaluate(() => document.getElementById('log')?.contains(document.activeElement));
    const onCall = await cursor();
    assert.match(onCall, /^row-/, 'clicking a row put the cursor on it');
    await page.keyboard.press('Escape');
    await out.waitFor({state: 'detached'});
    await page.keyboard.press('ArrowDown');
    const moved = await cursor();
    assert.notEqual(moved, onCall, 'Down moves the cursor');
    assert.match(moved, /^row-/, 'Down leaves the cursor on a row');
    await page.keyboard.press('j');
    assert.equal(await log.getAttribute('aria-label'), 'Round 2 log', 'the log kept its round');

    // The cursor never steps into a fold the reader cannot see, including the run fold nested
    // inside the collapsed orchestrator group.
    const hidden = await log.evaluate(node =>
      [...node.querySelectorAll('[data-row]')]
        .filter(row => !row.checkVisibility())
        .map(row => row.id),
    );
    assert.ok(hidden.length > 0, 'the fixture folds rows out of sight');
    await log.focus();
    await page.keyboard.press('ArrowDown');
    const walked = [];
    for (let step = 0; step < 12; step++) {
      walked.push(await cursor());
      await page.keyboard.press('ArrowDown');
    }
    assert.deepEqual(
      walked.filter(id => hidden.includes(id)),
      [],
      'the cursor stepped onto a row inside a closed fold',
    );

    // A trip out of the log and back returns to the row the cursor was on: the scroller is
    // the log's one tab stop, and every row and fold summary is out of the tab order.
    await call.focus();
    const stops = await log.evaluate(
      node => [...node.querySelectorAll('[tabindex="0"], summary:not([tabindex])')].length,
    );
    assert.equal(stops, 0, 'the log has a tab stop inside it');
    await page.keyboard.press('Tab');
    assert.equal(
      await page.evaluate(() => document.getElementById('log')?.contains(document.activeElement)),
      false,
      'Tab stayed inside the log',
    );
    await page.keyboard.press('Shift+Tab');
    assert.equal(await cursor(), await call.getAttribute('id'), 'Shift+Tab lost the cursor');
    // And out again: focus arriving from inside is Shift+Tab leaving, not a reader coming back,
    // so the log is not a one-way door. The scroller is the stop it steps back through.
    await page.keyboard.press('Shift+Tab');
    assert.equal(await cursor(), 'log', 'Shift+Tab from a row bounced back to it');
    // An arrow from the scroller resumes at the cursor rather than at the edge of the log.
    await page.keyboard.press('ArrowDown');
    assert.equal(await cursor(), await call.getAttribute('id'), 'the cursor restarted at the edge');
    await page.keyboard.press('Shift+Tab');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await inLog(), false, 'Shift+Tab could not leave the log');

    // Tab back into a log that has scrolled on brings the cursor into view, clear of the
    // sticky role header, the way an arrow key does.
    await call.focus();
    await page.keyboard.press('Tab');
    await log.evaluate(node => node.scrollTo(0, node.scrollHeight));
    await page.keyboard.press('Shift+Tab');
    const seen = await call.evaluate(node => {
      const box = node.getBoundingClientRect();
      const port = node.closest('.log').getBoundingClientRect();
      return [Math.round(box.top - port.top), box.bottom <= port.bottom];
    });
    assert.deepEqual(seen, [22, true], 'Tab back left the cursor out of view');
    // That scroll leaves the live edge, which mounts the pill and re-renders the log: let it
    // settle before measuring row positions below.
    await delay(150);

    // Enter on a tool row fills the inspector, and moves no row doing it.
    await call.focus();
    const beforeEnter = await belowTop();
    await page.keyboard.press('Enter');
    await out.waitFor();
    assert.equal(await belowTop(), beforeEnter, 'Enter on a tool row moved the row below it');
    await page.keyboard.press('ArrowLeft');
    // Left clears it again.
    await out.waitFor({state: 'detached'});

    // A render that hides the cursor's row must not leave focus on <body>, where the log's
    // keys are dead and the global handler reads the arrows as the rail's. Two renders do it.
    const recovered = async (what, id) => {
      const shown = await page.evaluate(
        row => document.getElementById(row)?.checkVisibility() ?? false,
        id,
      );
      assert.equal(shown, false, `${what} did not hide the cursor's row`);
      await recoveredFocus(what);
    };
    // Focus back inside the log, wherever it landed: on <body> the log's keys are dead and the
    // global handler reads the arrows as the rail's, so the next press would move the round.
    const recoveredFocus = async what => {
      assert.equal(await inLog(), true, `${what} left focus on ${await cursor()}`);
      await page.keyboard.press('ArrowUp');
      assert.equal(await log.getAttribute('aria-label'), 'Round 2 log', `${what} lost the round`);
    };
    // One: an adjacent call of the same verb settles, and the two fold into a run.
    push(ran(2, 'implementer', 'make alpha', 'alpha\n'));
    const alpha = log.locator('.row[data-tool]:visible', {hasText: 'make alpha'});
    await alpha.waitFor();
    await alpha.focus();
    const alphaId = await alpha.getAttribute('id');
    push(ran(2, 'implementer', 'make beta', 'beta\n'));
    await log.locator('details.fold > summary', {hasText: '2 calls'}).waitFor();
    await recovered('a run folding', alphaId);
    // Two: a new role speaks, and the group the cursor was in collapses behind its own fold.
    const last = log.locator('.row[data-tool]:visible').last();
    await last.focus();
    const lastId = await last.getAttribute('id');
    push(
      [event('phase_started', 2, 'judge', {kind: 'phase', phase: 'judge', attempt: null})],
      [implementer, {...implementer, execution_id: 'judge-2', agent_kind: 'judge', stage: 'judge'}],
    );
    await log.locator('.who', {hasText: 'Judge'}).waitFor();
    await recovered('a group collapsing', lastId);
    // Three: the cursor on a steer row when its group collapses. A collapsed group renders its
    // steers outside the fold, so that row is not hidden, it is built again from nothing: the
    // id still resolves, to a node that never had focus.
    const steered = text =>
      [
        {
          ...event('control', 2, 'implementer', null),
          status: 'pending',
          text: `/steer: ${text}`,
        },
        {...event('control', 2, 'implementer', null), status: 'consumed', text: '/steer'},
      ].map(({data, ...rest}) => rest);
    push([
      ...lines(2, 'implementer', 'Before the steer', 1),
      ...steered('Look at the harness'),
      ...lines(2, 'implementer', 'After the steer', 1),
    ]);
    const said = log.locator('.said:visible', {hasText: 'Look at the harness'});
    await said.waitFor();
    await said.focus();
    const saidId = await said.getAttribute('id');
    // Marked, so the assertion below can tell a node that was rebuilt from one that was kept.
    await page.evaluate(id => {
      document.getElementById(id).dataset.probe = 'held';
    }, saidId);
    push(
      [event('phase_started', 2, 'profiler', {kind: 'phase', phase: 'profiler', attempt: null})],
      [
        implementer,
        {...implementer, execution_id: 'profiler-2', agent_kind: 'profiler', stage: 'profiler'},
      ],
    );
    await log.locator('.who', {hasText: 'Profiler'}).waitFor();
    const after = await page.evaluate(id => {
      const row = document.getElementById(id);
      return row === null ? 'gone' : row.dataset.probe === 'held' ? 'kept' : 'rebuilt';
    }, saidId);
    assert.equal(after, 'rebuilt', 'the steer row was not remounted: this case tests nothing');
    await recoveredFocus('a steer remounting');

    // Away from the live edge the pill counts what arrived since, and `l` goes back to it.
    // Back at the edge first, so the count is taken from a mark this test set. `l` does it
    // whether or not the pill is up, which a click on the pill cannot.
    await page.keyboard.press('l');
    await delay(200);
    assert.equal(await pinned(), true, 'at the live edge before counting');
    await log.evaluate(node => node.scrollTo(0, 0));
    await jump.waitFor();
    push([
      ...ran(2, 'implementer', 'make one', 'one\n'),
      ...ran(2, 'implementer', 'make two', 'two\n'),
      ...ran(2, 'implementer', 'make three', 'three\n'),
    ]);
    await page.getByRole('button', {name: '3 new \u00b7 Jump to latest'}).waitFor();
    // `l` works from outside the log too.
    await page.getByRole('button', {name: 'Pause', exact: true}).focus();
    await page.keyboard.press('l');
    await jump.waitFor({state: 'detached'});
    await delay(150);
    assert.equal(await pinned(), true, 'l returned to the live edge');

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

    // Below 1024 px a row selection opens a modal dialog, which remembers the node it was
    // opened from and focuses it again on close. A commit behind it can replace that row, and
    // focus then lands on <body>, or on the dialog's own control now that the dialog is hidden.
    // Either way the log's keys are dead and the next arrow moves the round, and no React commit
    // attends the close, so the log cannot see it for itself.
    let delta = 0;
    const dialogKeepsTheRound = async (what, dialog) => {
      const verb = `Measured delta ${++delta}`;
      // A verb of its own, so it is a row rather than a member of the run fold beside it.
      push(ran(2, 'implementer', `make delta ${delta}`, 'delta\n', verb));
      const picked = log.locator('.row[data-tool]:visible', {hasText: verb});
      await picked.waitFor();
      await picked.focus();
      const pickedId = await picked.getAttribute('id');
      await page.keyboard.press('Enter');
      await dialog.waitFor();
      // Behind the dialog, a second call of that verb folds the row away.
      push(ran(2, 'implementer', `make delta ${delta} again`, 'delta\n', verb));
      await page.waitForFunction(
        id => !(document.getElementById(id)?.checkVisibility() ?? false),
        pickedId,
      );
      await page.keyboard.press('Escape');
      await dialog.waitFor({state: 'hidden'});
      // The hand-off runs in React's own `close` handling, a turn after the dialog hides.
      const landed = await page
        .waitForFunction(
          () => document.getElementById('log')?.contains(document.activeElement) ?? false,
          null,
          {timeout: 2000},
        )
        .then(
          () => true,
          () => false,
        );
      assert.equal(landed, true, `closing the ${what} left focus on ${await cursor()}`);
      await page.keyboard.press('ArrowUp');
      assert.equal(
        await log.getAttribute('aria-label'),
        'Round 2 log',
        `the ${what} lost the round`,
      );
    };
    // Drawer (768-1023 px) and sheet (below 768), which share the one expression that fixes it.
    await page.setViewportSize({width: 900, height: 1000});
    await dialogKeepsTheRound('drawer', drawer);
    await page.setViewportSize({width: 390, height: 844});
    await dialogKeepsTheRound('sheet', page.locator('dialog.insp-sheet'));
    await page.setViewportSize({width: 900, height: 1000});

    // An unknown change kind renders, and Esc on a tip hides only the tip.
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
    'Browser smoke passed: live events, pause/resume/steer, replay after refresh, completion, mobile layout, keyboard focus, round selection, run replacement, live follow, focus carry, tooltips, drawer, composer, context meter, tool output, row cursor, jump to live.',
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
