// Screenshot the built web UI in each B2 state, at 1440/1024/390 in dark and light.
//
// Replay (default): serves clients/web/dist with `vite preview` and mocks the gateway with
// Playwright routes, replaying recorded runs: clients/tui/dev/fixtures/queue-rs-payloads.jsonl
// (typed tool payloads, one round) and src/fixtures/stub-run.jsonl (8 rounds, a pause, a steer).
//   node clients/web/scripts/capture.mjs --out <dir> [--only live,paused] [--expect 'A|B']
//     [--dist <vite outDir>] [--strict]
// Live: drives a running stub harness (scratchpad live/run.sh) without mocks.
//   node clients/web/scripts/capture.mjs --out <dir> --live http://127.0.0.1:15173
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
import {chromium} from 'playwright';

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
const rail = (page, round) =>
  page.getByRole('navigation', {name: 'Rounds'}).getByRole('button', {name: new RegExp(`^R${round}\\b`)});

const REPLAY = [
  {name: 'loading', fixture: queue(QUEUE, {silent: true}), ready: null, expect: []},
  {
    name: 'live',
    fixture: live(),
    expect: [
      'queue-rs',
      'Optimize a single-producer, single-consumer bounded FIFO queue.',
      'Pause',
      'Measure baseline vs ring, 3 reps each',
      'Pending',
    ],
  },
  {
    name: 'pausing',
    fixture: queue(upTo(QUEUE, 623), {active: QUEUE_ACTIVE, status: 'pausing'}),
    widths: [1440],
    expect: ['Pausing'],
  },
  {name: 'paused', fixture: queue(upTo(QUEUE, 623), {status: 'paused'}), expect: ['Resume']},
  {
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
      await page.getByText('Reconnecting…').waitFor();
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
    expect: ['R3', 'H-03', 'Judge', 'Speed up the VALUE computation in candidate.py.'],
  },
  {
    // No recording has a baseline value: 900 is invented here to render the R0 row.
    name: 'baseline',
    fixture: stub({
      context: {...STUB_CONTEXT, objective_unit: 'median_tok_per_sec', objective_baseline_value: 900},
    }),
    widths: [1440, 390],
    after: page => rail(page, 1).click(),
    expect: ['R0', '+11.1%', 'vs R0'],
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
    expect: ['Keyboard shortcuts', 'Single-key shortcuts (j, k, p, /)'],
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
 * Answers /api/request and /api/events from a recorded run. With `tail`, a tailed subscribe gets
 * the gateway's shape (spine at or below the floor, then the newest 300 events) and
 * `query.events` serves the older chunks. `drop()` cuts the socket for good.
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
  await page.route('**/api/request', async route => {
    if (fixture.silent) return;
    const request = route.request().postDataJSON();
    const fields =
      request.type === 'query.snapshot'
        ? {snapshot}
        : request.type === 'query.experiments'
          ? {experiments_ready: true, experiments: fixture.experiments}
          : request.type === 'query.design'
            ? {design_ready: true, design: fixture.design}
            : request.type === 'query.performance'
              ? {performance: [], performance_context: fixture.context}
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
    await route.fulfill({
      json: failed
        ? {protocol_version: 1, request_id: request.request_id, ok: false, error: 'Backfill unavailable'}
        : {protocol_version: 1, request_id: request.request_id, ok: true, ...fields},
    });
  });
  let socket = null;
  let dropped = false;
  await page.routeWebSocket('**/api/events', ws => {
    if (dropped) {
      ws.close();
      return;
    }
    socket = ws;
    ws.onMessage(raw => {
      if (fixture.silent) return;
      const request = JSON.parse(raw);
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
      socket?.close();
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
  const vite = join(web, '../../node_modules/vite/bin/vite.js');
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
  await page.goto(origin);
  // Mocked requests settle at once; the socket replay is not network, so this waits for the queries.
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
