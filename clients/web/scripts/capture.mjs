// Screenshot the built web UI in each B2 state, at 1440/1024/390 in dark and light.
//
// Replay (default): serves clients/web/dist with `vite preview` and mocks the gateway's /ws
// sockets with Playwright, replaying recorded runs: clients/tui/dev/fixtures/queue-rs-payloads.jsonl
// (typed tool payloads, one round) and src/fixtures/stub-run.jsonl (8 rounds, a pause, a steer).
//   node clients/web/scripts/capture.mjs --out <dir> [--only live,paused] [--expect 'A|B']
//     [--dist <vite outDir>] [--strict]
// Live: drives a running gateway's capability URL without mocks.
//   node clients/web/scripts/capture.mjs --out <dir> --live 'http://127.0.0.1:8765/?token=...'
//
// --strict exits 1 when an expected text is missing, a step fails, the phone layout overflows,
// or the page throws. --expect replaces each scenario's expected texts ('|'-separated).
// PASS only means those checks held: open every PNG and compare it by eye.
import {spawn} from 'node:child_process';
import {mkdirSync, readFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
import {parseArgs} from 'node:util';
import {chromium} from '@playwright/test';

const web = fileURLToPath(new URL('../', import.meta.url));
const {values: args} = parseArgs({
  options: {
    out: {type: 'string'},
    only: {type: 'string'},
    live: {type: 'string'},
    expect: {type: 'string'},
    dist: {type: 'string'},
    strict: {type: 'boolean', default: false},
  },
});
if (!args.out) throw new Error('--out <dir> is required');
mkdirSync(args.out, {recursive: true});

const WIDTHS = [
  {width: 1440, height: 900},
  {width: 1024, height: 900},
  {width: 900, height: 900},
  {width: 390, height: 844},
];
const THEMES = ['dark', 'light'];

const text = path => readFileSync(join(web, path), 'utf8');
const jsonl = path => text(path).split('\n').filter(Boolean).map(line => JSON.parse(line));
const QUEUE = jsonl('../tui/dev/fixtures/queue-rs-payloads.jsonl');
const STUB = jsonl('src/fixtures/stub-run.jsonl');
const STUB_EXPERIMENTS = JSON.parse(text('src/fixtures/stub-experiments.json'));
const STUB_DESIGN = JSON.parse(text('src/fixtures/stub-design.json'));
const STUB_CONTEXT = JSON.parse(text('src/fixtures/stub-performance.json'));
// Event types the gateway replays below a tail floor (server/journal.py _BOOTSTRAP_SPINE_TYPES).
const SPINE = new Set([
  'run_started',
  'run_status_changed',
  'run_finished',
  'run_failed',
  'run_interrupted',
  'configuration_failed',
  'round_finished',
  'experiments_changed',
  'chat_thread_created',
]);
const upTo = (events, sequence) => events.filter(event => event.sequence <= sequence);
// query.performance answers with the same measurements the experiments rows carry: one row per
// round that recorded a value, which is what the rail's sparkline plots.
const perfRounds = experiments =>
  experiments
    .flatMap(entry => entry.rounds ?? [])
    .filter(round => typeof round.perf_metric === 'number')
    .map(round => ({
      round: round.round,
      perf_metric: round.perf_metric,
      perf_unit: round.perf_unit ?? '',
      passed: round.passed !== false,
    }));
const at = sequence => QUEUE.find(event => event.sequence === sequence);

// queue-rs at sequence 623: the implementer's benchmark call is in flight.
const implementer = at(261);
const plan = at(250).data.args;
// The run's objective as its orchestrator read it (the result of call 49).
const objective = QUEUE.find(
  event => event.data?.kind === 'tool_result' && event.data.call_id === at(49).data.call_id,
).data.content;
const QUEUE_ACTIVE = [
  {
    execution_id: implementer.execution_id,
    agent_kind: 'implementer',
    round_label: implementer.round_label,
    stage: 'implementer',
    attempt: 1,
    assignment: '',
    started_at: implementer.timestamp,
    activity: {mode: 'tool', summary: 'Running a command', tool: 'Bash'},
  },
];
// The orchestrator's recorded round-1 plan is the only hypothesis this recording has.
const QUEUE_EXPERIMENTS = [
  {
    hypothesis_id: plan.hypothesis_id,
    title: plan.title,
    claim: plan.hypothesis,
    first_round: 1,
    last_round: 1,
    rounds: [],
    active: true,
  },
];
const queue = (events, extra = {}) => ({
  runId: QUEUE[0].run_id,
  events,
  status: 'running',
  active: [],
  experiments: QUEUE_EXPERIMENTS,
  design: [],
  context: {objective_description: objective},
  clock: '2026-08-31T21:27:00Z',
  ...extra,
});
const live = () => queue(upTo(QUEUE, 623), {active: QUEUE_ACTIVE});
const stub = extra => ({
  runId: STUB[0].run_id,
  events: STUB,
  status: 'completed',
  active: [],
  experiments: STUB_EXPERIMENTS,
  design: STUB_DESIGN,
  context: STUB_CONTEXT,
  clock: '2026-09-21T20:36:00Z',
  ...extra,
});
// The stub run at sequence 212: paused in round 3 with one steer queued.
const queued = () =>
  stub({
    events: STUB.filter(event => event.sequence <= 212),
    status: 'paused',
    experiments: STUB_EXPERIMENTS.filter(entry => entry.last_round < 3),
    design: STUB_DESIGN.filter(round => round.round < 3),
    clock: '2026-09-21T20:34:43Z',
  });
// An invented loop shape, the way the `baseline` scenario invents a baseline value: the agent
// loop's four roles plus `perf_eval`, which belongs to the `plain` loop. No recording runs five
// roles in a round, and five is what it takes to overflow the graph row at 1440.
const FIVE_ROLES = ['orchestrator', 'implementer', 'judge', 'profiler', 'perf_eval'];
// queue-rs round 1 with its first orchestrator call failed rather than completed. Cards collapse
// only when they say the same thing, so the retry stays a second card and the round reads
// orchestrator, orchestrator, implementer: what a retry really looks like. The recording completed
// both calls, so the failure is invented, the way `baseline` invents a baseline value.
const RETRY = events =>
  events.map(event => (event.sequence === 128 ? {...event, status: 'failed'} : event));
// The implementer's later calls relabelled as a second attempt, so the log's retry badge has a
// frame. No recording carries a `-retry-K-` round label, which is the only thing that says an
// attempt repeated, so this is invented the way `baseline` invents a baseline value.
const RETRY_LABEL = events =>
  events.map(event =>
    event.agent_kind === 'implementer' && event.sequence > 430
      ? {...event, round_label: 'round-1-retry-2-implementer'}
      : event,
  );
// Every usage_update in the recording carries 2 input tokens against a 200k window, so the live
// frames show `2/200k context` and nothing shows the meter near its ceiling. This raises the
// count the way `baseline` invents a baseline value; the window stays the recording's.
const NEAR_FULL = events =>
  events.map(event =>
    event.data?.kind === 'usage_update'
      ? {...event, data: {...event.data, input_tokens: 184_320}}
      : event,
  );
// Rounds 9 to 32, cloned from the stub's last hypothesis with a rising metric. No recording has
// enough rounds to scroll the rail at 900px tall, which is the state that pins the trend.
const MANY_ROUNDS = [
  ...STUB_EXPERIMENTS,
  ...Array.from({length: 24}, (unused, index) => {
    const last = STUB_EXPERIMENTS.at(-1);
    const round = 9 + index;
    return {
      ...last,
      hypothesis_id: `H-${String(round).padStart(2, '0')}`,
      first_round: round,
      last_round: round,
      rounds: [{...last.rounds.at(-1), round, perf_metric: 1320 + index * 6}],
    };
  }),
];
const rail = (page, round) =>
  page.getByRole('navigation', {name: 'Rounds'}).getByRole('button', {name: new RegExp(`^R${round}\\b`)});

const REPLAY = [
  {name: 'loading', fixture: queue(QUEUE, {silent: true}), ready: null, expect: []},
  {
    name: 'live',
    fixture: live(),
    // The graph's four cards fit at 1440 and 1024 (the inspector is a drawer there) and not at
    // 390. Only the row's own ResizeObserver can notice the narrower scrollport; without it the
    // row overflows at 390 with no fade to say so.
    each: async (page, width) => {
      // The fade follows the observer, so it lands a frame or two after the resize.
      const wanted = width === 390 ? 'end' : null;
      await page
        .waitForFunction(
          want => (document.querySelector('.gflow')?.getAttribute('data-more') ?? null) === want,
          wanted,
          {timeout: 3000},
        )
        .catch(() => {
          throw new Error(`the fade at ${width} never became ${wanted}`);
        });
    },
    expect: [
      'queue-rs',
      'Optimize a single-producer, single-consumer bounded FIFO queue.',
      'Pause',
      'Measure baseline vs ring, 3 reps each',
      'Measured when the round finishes',
      // The recording's own usage_update, in the header's context meter.
      '2 of 200k context',
      // Graph only: a role the round has not reached, and the model the live agent runs on.
      'Profiler',
      'claude-opus-5',
    ],
  },
  {
    // A tool row's full output in the inspector, the one thing the web had no path to.
    name: 'output',
    fixture: live(),
    widths: [1440],
    after: async page => {
      await page.locator('#log .row[data-tool]:visible').first().click();
      await page.locator('.insp .out').waitFor();
    },
    // Both lines are past the objective's first sentence, so only this section renders them:
    // the header shows the first sentence and keeps the rest in a tooltip. The heading is not
    // checked here, because the same command also sits in a collapsed fold, where `getByText`
    // finds it first and reports it hidden; the browser check asserts the heading by scope.
    expect: ['Headline metric:', 'Preserve the required interface:'],
  },
  {
    // The context meter near its ceiling, which no recording reaches.
    name: 'context',
    fixture: queue(NEAR_FULL(upTo(QUEUE, 623)), {active: QUEUE_ACTIVE}),
    widths: [1440, 390],
    expect: ['184k of 200k context'],
  },
  {
    // Five roles overflow the graph row at every width, which is what the edge fade marks.
    name: 'many-agents',
    fixture: queue(
      upTo(QUEUE, 623).map(event =>
        event.data?.kind === 'run_started'
          ? {...event, data: {...event.data, expected_roles: FIVE_ROLES}}
          : event,
      ),
      {active: QUEUE_ACTIVE},
    ),
    widths: [1440],
    expect: ['Profiler', 'Perf eval'],
  },
  {
    // A retry: two orchestrator cards, and one handover out of the second one.
    name: 'retry-chain',
    fixture: queue(RETRY(upTo(QUEUE, 623)), {active: QUEUE_ACTIVE}),
    widths: [1440],
    expect: ['Failed', 'Completed', 'Running'],
  },
  {
    // The log's side of a retry: the badge on each group header of the second attempt, and no
    // divider row anywhere.
    name: 'retry-log',
    fixture: queue(RETRY_LABEL(upTo(QUEUE, 623)), {active: QUEUE_ACTIVE}),
    widths: [1440],
    after: page => page.locator('#log .who', {hasText: 'Attempt 2'}).waitFor(),
    expect: ['Attempt 2'],
  },
  {
    // The vertical fade and the tab stop that comes with it. No round reaches a second row: the
    // inferred chain is one card per rank, and the shapes that stack (a fan-out, a join) need
    // edges the backend does not report yet. So this forces the state the general path handles,
    // by capping the panel under its own one row, and checks the panel notices it on the vertical
    // axis as it does sideways.
    //
    // 40px, against a 44px card: the fade covers the last 24px, so it runs across the card's
    // second line and that line dissolves while the role above it stays crisp. A tighter cap
    // leaves the fade nothing but the card's own fill to work on, and surface against canvas is
    // 8 levels of luminance in the dark theme and 4 in the light one: measurable, invisible.
    name: 'panel-cap',
    fixture: live(),
    widths: [1440],
    after: async page => {
      await page.locator('.gflow').evaluate(panel => {
        panel.style.maxHeight = '40px';
      });
      await page
        .waitForFunction(
          () => {
            const panel = document.querySelector('.gflow');
            return panel?.getAttribute('data-down') === 'end' && panel.getAttribute('tabindex') === '0';
          },
          null,
          {timeout: 3000},
        )
        .catch(() => {
          throw new Error('the panel never noticed it scrolls down');
        });
    },
    expect: ['Orchestrator'],
  },
  {
    name: 'pausing',
    fixture: queue(upTo(QUEUE, 623), {active: QUEUE_ACTIVE, status: 'pausing'}),
    widths: [1440],
    expect: ['Pausing'],
  },
  {name: 'paused', fixture: queue(upTo(QUEUE, 623), {status: 'paused'}), expect: ['Resume']},
  {
    // The pill, and the row under it: the column reserves the pill's band, so nothing is
    // covered by it.
    name: 'scrolled',
    fixture: live(),
    after: page => page.locator('#log').evaluate(node => node.scrollTo(0, 0)),
    each: page => page.locator('#log').evaluate(node => node.scrollTo(0, 0)),
    expect: ['Jump to latest'],
  },
  {
    name: 'disconnected',
    fixture: live(),
    after: async (page, gateway) => {
      gateway.drop();
      await page.getByText('Reconnecting…').first().waitFor();
    },
    expect: ['Reconnecting…'],
  },
  {
    name: 'offline',
    fixture: live(),
    after: async (page, gateway) => {
      gateway.drop();
      await page.getByText('Disconnected from the backend.').waitFor({timeout: 40_000});
    },
    expect: ['Disconnected from the backend.', 'Retry'],
  },
  {name: 'ended', fixture: queue(QUEUE, {status: 'failed'}), expect: ['Interrupted']},
  {
    name: 'r3',
    fixture: stub(),
    after: page => rail(page, 3).click(),
    // 'Stub' is the graph's runtime label for this recording's harness; nothing else shows it.
    expect: ['R3', 'H-03', 'Judge', 'Speed up the VALUE computation in candidate.py.', 'Stub'],
  },
  {
    // No recording has a baseline value: 900 is invented here to render the R0 row.
    name: 'baseline',
    fixture: stub({
      context: {...STUB_CONTEXT, objective_unit: 'median_tok_per_sec', objective_baseline_value: 900},
    }),
    widths: [1440, 390],
    after: async page => {
      await rail(page, 1).click();
      // The curve starts from the invented baseline, so this is the frame that shows R0 on it.
      await page.getByRole('img', {name: 'Metric trend: 900 to 1,315, R0 to R8'}).waitFor();
    },
    expect: ['R0', '+11.1%', 'vs R0'],
  },
  {
    // 32 rounds overflow the rail at 900px tall. The rows scroll; the trend and the rounds-left
    // line stay at the bottom of the rail.
    name: 'many-rounds',
    fixture: stub({experiments: MANY_ROUNDS}),
    widths: [1440],
    after: page => page.getByRole('img', {name: /^Metric trend: .* R1 to R32$/}).waitFor(),
    each: page => page.locator('.rail-rows').evaluate(rows => rows.scrollTo(0, rows.scrollHeight)),
    expect: ['R32'],
  },
  {
    // A 300-event tail hides rounds 1-3; selecting R3 backfills once and shows its steer.
    name: 'backfill',
    fixture: stub({tail: true}),
    widths: [1440],
    after: async (page, gateway) => {
      await rail(page, 3).click();
      await page.getByText('Try caching VALUE instead of recomputing it.').waitFor();
      if (gateway.eventQueries() !== 1) {
        throw new Error(`expected 1 backfill request, saw ${gateway.eventQueries()}`);
      }
    },
    expect: ['Try caching VALUE instead of recomputing it.'],
  },
  {
    name: 'backfill-error',
    fixture: stub({tail: true, backfillFails: true}),
    widths: [1440],
    after: async (page, gateway) => {
      await rail(page, 3).click();
      await page.getByText('Could not load earlier events: Backfill unavailable').waitFor();
      await delay(1000);
      if (gateway.eventQueries() !== 1) {
        throw new Error(`expected no automatic retry, saw ${gateway.eventQueries()} requests`);
      }
    },
    expect: ['Could not load earlier events: Backfill unavailable', 'Retry'],
  },
  {
    // 768-1023 px: selecting a round opens the inspector as a right drawer.
    name: 'drawer',
    fixture: stub(),
    widths: [900],
    each: page => rail(page, 3).click(),
    expect: [],
  },
  {
    name: 'queued',
    fixture: queued(),
    expect: ['Queued', 'Try caching VALUE instead of recomputing it.', 'Resume'],
  },
  {
    name: 'hover',
    fixture: stub(),
    widths: [1440],
    after: page => page.locator('.rrow .inc').first().hover(),
    expect: ['Incumbent: the best result so far. New rounds are compared against it.'],
  },
  {
    name: 'keys',
    fixture: live(),
    widths: [1440],
    after: page => page.keyboard.press('?'),
    expect: ['Keyboard shortcuts', 'Move the cursor in the log', 'Single-key shortcuts (j, k, l, p, /)'],
  },
  {
    name: 'sheet',
    fixture: live(),
    widths: [390],
    each: page => rail(page, 1).click(),
    expect: ['Lock-free SPSC ring over preallocated slab'],
  },
];

const LIVE = [
  {
    name: 'live',
    after: page => page.getByRole('button', {name: 'Pause'}).waitFor({timeout: 60_000}),
    expect: ['Pause'],
  },
  {
    name: 'r3',
    after: async page => {
      await rail(page, 3).waitFor({timeout: 180_000});
      await rail(page, 3).click();
    },
    expect: ['R3'],
  },
  {
    name: 'paused',
    after: async page => {
      await page.getByRole('button', {name: 'Pause'}).click();
      await page.getByRole('button', {name: 'Resume'}).waitFor({timeout: 60_000});
    },
    expect: ['Resume'],
  },
  {
    name: 'ended',
    after: async page => {
      await page.getByRole('button', {name: 'Resume'}).click();
      await page.getByText('Completed', {exact: true}).waitFor({timeout: 600_000});
    },
    expect: ['Completed'],
  },
];

/**
 * Answers the gateway's `/ws` sockets from a recorded run: `subscribe` frames get the replay,
 * every other frame is a request. With `tail`, a tailed subscribe gets the gateway's shape
 * (spine at or below the floor, then the newest 300 events) and `query.events` serves the older
 * chunks. `drop()` cuts the event stream for good.
 */
async function mockGateway(page, fixture) {
  const last = fixture.events.at(-1)?.sequence ?? 0;
  let eventQueries = 0;
  const snapshot = {
    run_id: fixture.runId,
    sequence: last,
    status: fixture.status,
    active_executions: fixture.active,
  };
  const answer = request => {
    const fields =
      request.type === 'query.snapshot'
        ? {snapshot}
        : request.type === 'query.experiments'
          ? {experiments_ready: true, experiments: fixture.experiments}
          : request.type === 'query.design'
            ? {design_ready: true, design: fixture.design}
            : request.type === 'query.performance'
              ? {performance: perfRounds(fixture.experiments), performance_context: fixture.context}
              : request.type === 'query.events'
                ? {
                    events: fixture.events.filter(
                      event =>
                        event.sequence > request.after_sequence &&
                        (request.before_sequence == null || event.sequence < request.before_sequence),
                    ),
                  }
                : request.type.startsWith('command.')
                  ? {ack: {action: request.type.slice('command.'.length), status: 'pending'}}
                  : {};
    if (request.type === 'query.events') eventQueries += 1;
    const failed = request.type === 'query.events' && fixture.backfillFails;
    return failed
      ? {protocol_version: 1, request_id: request.request_id, ok: false, error: 'Backfill unavailable'}
      : {protocol_version: 1, request_id: request.request_id, ok: true, ...fields};
  };
  const streams = new Set();
  let dropped = false;
  await page.routeWebSocket(/\/ws(\?|$)/, ws => {
    ws.onMessage(raw => {
      if (fixture.silent) return;
      const request = JSON.parse(raw);
      if (request.type !== 'subscribe') {
        ws.send(JSON.stringify(answer(request)));
        return;
      }
      if (dropped) {
        ws.close();
        return;
      }
      streams.add(ws);
      const floor = fixture.tail && request.tail ? Math.max(0, last - request.tail) : 0;
      const replay = {
        type: 'event_batch',
        events:
          request.after_sequence === 0
            ? fixture.events.filter(event => event.sequence > floor || SPINE.has(event.type))
            : [],
        through_sequence: last,
        active_executions: fixture.active,
        history_after_sequence: floor,
      };
      ws.send(
        JSON.stringify({
          type: 'subscribed',
          request_id: request.request_id,
          run_id: fixture.runId,
          latest_sequence: last,
        }),
      );
      ws.send(JSON.stringify(replay));
    });
  });
  return {
    drop() {
      dropped = true;
      for (const ws of streams) ws.close();
    },
    eventQueries: () => eventQueries,
  };
}

async function freePort() {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const {port} = server.address();
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function preview() {
  const port = await freePort();
  const vite = join(web, '../node_modules/vite/bin/vite.js');
  const child = spawn(
    process.execPath,
    [
      vite,
      'preview',
      '--host',
      '127.0.0.1',
      '--port',
      String(port),
      '--strictPort',
      ...(args.dist ? ['--outDir', args.dist] : []),
    ],
    {cwd: web, stdio: 'ignore'},
  );
  const origin = `http://127.0.0.1:${port}`;
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      if ((await fetch(origin)).ok) return {origin, child};
    } catch {
      // not listening yet
    }
    await delay(100);
  }
  child.kill();
  throw new Error('vite preview did not start; run `pnpm --filter @vibesys/web build` first');
}

async function capture(browser, origin, scenario) {
  const failures = [];
  const context = await browser.newContext({
    viewport: WIDTHS[0],
    colorScheme: 'dark',
    reducedMotion: 'reduce',
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => failures.push(`page error: ${error.message}`));
  const gateway = scenario.fixture ? await mockGateway(page, scenario.fixture) : null;
  if (scenario.fixture) await page.clock.setFixedTime(new Date(scenario.fixture.clock));
  const step = async (name, run) => {
    try {
      await run();
    } catch (error) {
      failures.push(`${name} failed: ${error.message.split('\n')[0]}`);
    }
  };
  // The replay page needs a capability token to take the live path; the mock accepts any.
  await page.goto(scenario.fixture ? `${origin}/?token=capture` : origin);
  // Mocked sockets are not network, so this only waits for the page's own assets.
  if (scenario.ready !== null) await page.waitForLoadState('networkidle');
  await step('after', () => scenario.after?.(page, gateway));
  for (const text of args.expect?.split('|') ?? scenario.expect) {
    if (!(await page.getByText(text).first().isVisible())) failures.push(`missing text: ${text}`);
  }
  const widths = WIDTHS.filter(size =>
    scenario.widths ? scenario.widths.includes(size.width) : size.width !== 900,
  );
  for (const size of widths) {
    await page.setViewportSize(size);
    await step(`each at ${size.width}`, () => scenario.each?.(page, size.width));
    if (size.width === 390) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      if (overflow) failures.push('horizontal overflow at 390');
    }
    for (const theme of THEMES) {
      await page.emulateMedia({colorScheme: theme});
      await delay(150);
      await page.screenshot({path: join(args.out, `${scenario.name}-${theme}-${size.width}.png`)});
    }
  }
  await context.close();
  return failures;
}

const only = args.only?.split(',');
const scenarios = (args.live ? LIVE : REPLAY).filter(item => !only || only.includes(item.name));
const server = args.live ? null : await preview();
let browser;
let failed = false;
try {
  browser = await chromium.launch();
  for (const scenario of scenarios) {
    const failures = await capture(browser, args.live ?? server.origin, scenario);
    failed ||= failures.length > 0;
    console.log(`${failures.length === 0 ? 'PASS' : 'FAIL'} ${scenario.name}`);
    for (const failure of failures) console.log(`  ${failure}`);
  }
} finally {
  await browser?.close();
  server?.child.kill();
}
console.log(`Frames in ${args.out}. PASS checks text and overflow only; review each PNG.`);
if (failed && args.strict) process.exit(1);
