import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {type Gateway, HomeError, type RunRow} from './home-api.js';
import {
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
});

test('launch_failed and an unreachable home fail with the tail; other refusals are messages', () => {
  const failed = launchError(
    new HomeError('launch_failed', 'exited 1', {stderr_tail: ['a', 'b'], stderr_log: '/l'}),
  );
  assert.deepEqual(failed, {
    kind: 'failed',
    failure: {message: 'exited 1', tail: ['a', 'b'], log: '/l'},
  });
  assert.equal(launchError(new HomeError('network', 'down', null)).kind, 'failed');
  const live = launchError(
    new HomeError('already_live', 'The project already has a live run', {run_id: 'r0'}),
  );
  assert.deepEqual(launchLine(live), {busy: null, error: 'The project already has a live run'});
  const smaller = launchError(
    new HomeError('budget_decrease', 'budget below recorded', {recorded: 12}),
  );
  assert.equal(
    launchLine(smaller).error,
    'The run already has a budget of 12; resume with at least that.',
  );
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
