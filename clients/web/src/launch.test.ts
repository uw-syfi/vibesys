import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {type Gateway, HomeError, homeClient, type RunRow} from './home-api.js';
import {
  followLaunch,
  gatewayPhase,
  launchError,
  launchLine,
  launchPhase,
  locationText,
  tailParts,
} from './launch.js';

const gateway = (state: Gateway['state'], patch: Partial<Gateway> = {}): Gateway => ({
  state,
  url: null,
  websocket_url: null,
  token: null,
  stderr_tail: [],
  stderr_log: null,
  origin_mismatch: false,
  ...patch,
});
const row = (state: Gateway['state'], patch: Partial<Gateway> = {}): RunRow => ({
  run_id: 'r1',
  loop: 'agent',
  status: 'active',
  rounds: 0,
  gateway: gateway(state, patch),
  reopen: null,
  error: null,
  task: null,
  objective: null,
  created_at: null,
});
const WS = 'ws://127.0.0.1:53211/ws?token=gw';

test('a launch waits while starting and is ready once a serving gateway has a socket', () => {
  assert.deepEqual(launchPhase(undefined), {kind: 'waiting'});
  assert.deepEqual(launchPhase(row('starting', {websocket_url: WS})), {kind: 'waiting'});
  assert.deepEqual(launchPhase(row('live', {websocket_url: WS})), {
    kind: 'ready',
    websocketUrl: WS,
  });
  assert.deepEqual(launchPhase(row('ended_serving', {websocket_url: WS})), {
    kind: 'ready',
    websocketUrl: WS,
  });
  assert.deepEqual(gatewayPhase(gateway('reopened', {websocket_url: WS}), null), {
    kind: 'ready',
    websocketUrl: WS,
  });
});

test('a run server that died reports its stderr; a vanished one says so', () => {
  assert.deepEqual(launchPhase(row('failed', {stderr_tail: ['boom'], stderr_log: '/x.log'})), {
    kind: 'failed',
    failure: {
      message: 'The run server exited before the run started.',
      tail: ['boom'],
      log: '/x.log',
    },
  });
  assert.equal(launchPhase(row('none')).kind, 'failed');
  assert.equal(launchPhase(row('stale')).kind, 'failed');
  // A run the run store has recorded got past start-up: its baseline failed.
  const baseline = launchPhase({...row('failed'), task: 'decode'});
  assert.equal(
    baseline.kind === 'failed' && baseline.failure.message,
    'The baseline benchmark failed.',
  );
});

test('launch_failed and an unreachable home fail with the tail; other refusals are messages', () => {
  const failed = launchError(
    new HomeError('launch_failed', 'exited 1', {stderr_tail: ['a', 'b'], stderr_log: '/l'}),
  );
  assert.deepEqual(failed, {
    kind: 'failed',
    failure: {message: 'Exited 1.', tail: ['a', 'b'], log: '/l'},
  });
  assert.equal(launchError(new HomeError('network', 'down', null)).kind, 'failed');
  assert.deepEqual(launchError(new Error('boom')), {
    kind: 'failed',
    failure: {message: 'Boom.', tail: [], log: null},
  });
  const live = launchError(
    new HomeError('already_live', 'The project already has a live run', {run_id: 'r0'}),
  );
  assert.deepEqual(launchLine(live), {
    busy: null,
    error: 'Run r0 is still live in this project; open it and stop it first.',
  });
  const external = launchError(new HomeError('already_live', 'live', {run_id: null}));
  assert.equal(
    launchLine(external).error,
    'Another launcher has a run live in this project; stop it there first.',
  );
  const smaller = launchError(
    new HomeError('budget_decrease', 'budget below recorded', {recorded: 12}),
  );
  assert.equal(
    launchLine(smaller).error,
    'The run already has a budget of 12; resume with at least that.',
  );
  const gone = launchError(new HomeError('unknown_run', 'no run r0', null));
  assert.equal(launchLine(gone).error, 'This run no longer exists.');
  assert.deepEqual(launchLine({kind: 'sending'}), {busy: 'Launching the run server…', error: null});
  assert.deepEqual(launchLine({kind: 'starting', runId: 'r1'}), {
    busy: 'Starting the run…',
    error: null,
  });
  assert.deepEqual(launchLine({kind: 'idle'}), {busy: null, error: null});
});

test('stderr lines split into text and file locations (Rust and Python forms)', () => {
  assert.deepEqual(tailParts('  --> benches/decode.rs:41:14'), [
    '  --> ',
    {path: 'benches/decode.rs', line: 41, column: 14, text: 'benches/decode.rs:41:14'},
  ]);
  assert.deepEqual(tailParts('  File "/Users/me/vibesys/src/config.py", line 214, in load'), [
    '  ',
    {
      path: '/Users/me/vibesys/src/config.py',
      line: 214,
      column: null,
      text: 'File "/Users/me/vibesys/src/config.py", line 214',
    },
    ', in load',
  ]);
  assert.deepEqual(tailParts('error: could not compile `llm-serve` (bench "decode")'), [
    'error: could not compile `llm-serve` (bench "decode")',
  ]);
  assert.deepEqual(tailParts('   Compiling llm-serve v0.1.0 (/Users/me/src/llm-serve)'), [
    '   Compiling llm-serve v0.1.0 (/Users/me/src/llm-serve)',
  ]);
  const [, relative] = tailParts('--> benches/decode.rs:41:14');
  assert.ok(typeof relative === 'object');
  assert.equal(
    locationText('/Users/me/src/llm-serve', relative),
    '/Users/me/src/llm-serve/benches/decode.rs:41:14',
  );
  assert.equal(locationText(null, relative), 'benches/decode.rs:41:14');
});

const STARTING = {run_id: 'r1', gateway: gateway('starting')};
const now = async () => undefined;

test('a launch keeps waiting past 120 polls while the gateway is starting', async () => {
  let polls = 0;
  const client = homeClient('t', async () => {
    polls += 1;
    const state = polls <= 150 ? 'starting' : 'live';
    return Response.json({runs: [row(state, {websocket_url: WS})]});
  });
  const phase = await followLaunch(client, 'p', STARTING, () => true, now);
  assert.deepEqual(phase, {kind: 'ready', websocketUrl: WS});
  assert.equal(polls, 151);
});

test('a poll the home server did not answer keeps waiting; the next one attaches', async () => {
  const replies: Array<() => Response> = [
    () => {
      throw new TypeError('Failed to fetch');
    },
    () => Response.json({runs: [row('live', {websocket_url: 'ws://gw'})]}),
  ];
  const client = homeClient('t', async () => {
    const reply = replies.shift();
    assert.ok(reply, 'polled after the run attached');
    return reply();
  });
  assert.deepEqual(await followLaunch(client, 'p', STARTING, () => true, now), {
    kind: 'ready',
    websocketUrl: 'ws://gw',
  });
});

test('a refused poll ends the launch; a page that left stops polling', async () => {
  const refused = homeClient('t', async () =>
    Response.json({error: {code: 'unauthorized', message: 'no', details: null}}, {status: 401}),
  );
  await assert.rejects(
    followLaunch(refused, 'p', STARTING, () => true, now),
    HomeError,
  );
  let polls = 0;
  const counted = homeClient('t', async () => {
    polls += 1;
    return Response.json({runs: []});
  });
  assert.equal((await followLaunch(counted, 'p', STARTING, () => polls < 3, now)).kind, 'waiting');
  assert.equal(polls, 3);
});
