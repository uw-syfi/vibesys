// A one-command, no-backend demo: builds nothing itself (run `build` first, see
// package.json's `predemo`) and serves the built UI at http://127.0.0.1:5173,
// replaying clients/web/src/fixtures/demo-run.jsonl (an eight-round llm-serve
// decode-throughput optimization: judge verdicts, a reverted round, a judge
// rejection, an improving-but-non-monotonic metric, real tool calls) through
// an in-process mock of the browser gateway (src/server/browser_gateway.py):
// same /api/request + /api/events contract, same origin as the static page,
// so no proxy is needed.
//
// The first five rounds land immediately on connect; the rest stream in at a
// human pace. The eighth round never finishes: once its scripted beats run
// out the server keeps synthesizing plausible tool calls for it, so the run
// stays `running` indefinitely and Pause/Resume/Steer have something to act
// on, the way they would against a real long-running run.
//
// Run with: pnpm --filter @vibesys/web demo
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const web = fileURLToPath(new URL('../', import.meta.url));
const dist = join(web, 'dist');
const text = path => readFileSync(join(web, path), 'utf8');
const jsonl = path => text(path).split('\n').filter(Boolean).map(line => JSON.parse(line));

const STATIC_EVENTS = jsonl('src/fixtures/demo-run.jsonl');
const EXPERIMENTS = JSON.parse(text('src/fixtures/demo-experiments.json'));
const DESIGN = JSON.parse(text('src/fixtures/demo-design.json'));
const CONTEXT = JSON.parse(text('src/fixtures/demo-performance.json'));
const RUN_ID = STATIC_EVENTS[0].run_id;

// The final round's story runs out mid-implementer; these are the plausible
// beats the server loops over so round 8 looks like it is still working.
const ROUND8_LOOP = [
  {
    tool: 'Bash',
    args: {command: "rg -n 'fn sample' src/sampler.rs", description: 'Re-check the sampling entry point'},
    stdout: 'src/sampler.rs:60:fn sample_token(seq: &mut Sequence) {',
  },
  {
    tool: 'Bash',
    args: {command: 'cargo test --release batch 2>&1 | tail -8', description: 'Run batch tests against the in-progress overlap change'},
    stdout: 'running 9 tests\ntest result: ok. 9 passed; 0 failed; 0 ignored',
    duration: 11.4,
  },
  {prose: 'Tests still pass with the second stream in place. Measuring the overlap before wiring it into the main loop.'},
  {
    tool: 'Bash',
    args: {command: 'cargo bench --bench decode -- --quick 2>&1 | tail -5', description: 'Quick benchmark of the overlapped path'},
    stdout: 'decode_throughput      time:   [0.744 ms 0.751 ms 0.760 ms]\nmedian_tok_per_sec: 1327.8 (provisional)',
    duration: 14.9,
  },
  {prose: 'Provisional number looks promising; running a longer benchmark to confirm before reporting.'},
  {
    tool: 'Bash',
    args: {command: "rg -n 'fn prefill_next' src/batch.rs", description: 'Double-check prefill does not read sampler state'},
    stdout: 'src/batch.rs:230:fn prefill_next(&mut self, batch: &DecodeBatch) {',
  },
];
const FINAL_ROUND_LABEL = 'round-8-retry-1-implementer';
const FINAL_EXECUTION_ID = STATIC_EVENTS.find(event => event.round_label === FINAL_ROUND_LABEL)?.execution_id;

// --- the live timeline: everything actually sent to a client, in the order --
// it was sent, and the single counter that numbers all of it (both the
// canned story and anything synthesized later), so sequence stays strictly
// increasing across the two sources.
const timeline = [];
let seq = 0;
const sockets = new Set();
let paused = false;
let pauseTimer = null;

function nextSequence() {
  seq += 1;
  return seq;
}

/** Assigns the next sequence and timestamp, appends to the timeline, and broadcasts it alone. */
function emit(partial) {
  const event = {
    protocol_version: 1,
    sequence: nextSequence(),
    run_id: RUN_ID,
    timestamp: new Date().toISOString(),
    text: '',
    diagnostic: null,
    status: null,
    round_label: null,
    agent_kind: null,
    invocation_id: null,
    execution_id: null,
    chat_thread_id: null,
    data: null,
    ...partial,
  };
  timeline.push(event);
  broadcast([event]);
  return event;
}

function broadcast(events) {
  if (events.length === 0 || sockets.size === 0) return;
  const message = JSON.stringify({
    type: 'event_batch',
    events,
    through_sequence: timeline.at(-1).sequence,
    history_after_sequence: 0,
  });
  for (const ws of sockets) ws.send(message);
}

// --- reveal clock: the canned story first, then an endless round 8 --------
// Rounds 1-5 land in one burst so a fresh connection already shows real
// progress; rounds 6-8's beats trickle in afterward at a human pace. Once
// the story runs out, round 8 keeps generating plausible tool calls.
const burstEnd =
  STATIC_EVENTS.findIndex(event => event.type === 'round_finished' && event.round_label === 'round-5') + 1;
let storyIndex = 0;
let roundLabel = null;
let agentKind = null;

function revealStatic(event) {
  const cloned = {...event, sequence: nextSequence(), timestamp: new Date().toISOString()};
  timeline.push(cloned);
  if (cloned.round_label) roundLabel = cloned.round_label;
  if (cloned.agent_kind) agentKind = cloned.agent_kind;
  return cloned;
}

function revealBurst() {
  const batch = [];
  while (storyIndex < burstEnd) {
    batch.push(revealStatic(STATIC_EVENTS[storyIndex]));
    storyIndex += 1;
  }
  return batch;
}

let loopBeat = 0;
function synthesizeRound8Pair() {
  const beat = ROUND8_LOOP[loopBeat % ROUND8_LOOP.length];
  loopBeat += 1;
  const shared = {round_label: FINAL_ROUND_LABEL, agent_kind: 'implementer', invocation_id: FINAL_EXECUTION_ID, execution_id: FINAL_EXECUTION_ID};
  if (beat.prose) {
    emit({type: 'output', ...shared, data: {kind: 'agent_output_chunk', channel: 'assistant', content: beat.prose}});
    return;
  }
  const callId = `round8-loop-${loopBeat}`;
  emit({
    type: 'tool_call',
    ...shared,
    data: {kind: 'tool_call', tool: beat.tool, call_id: callId, args: beat.args},
  });
  emit({
    type: 'tool_result',
    ...shared,
    data: {
      kind: 'tool_result',
      tool: beat.tool,
      call_id: callId,
      content: beat.stdout,
      is_error: false,
      payload: {kind: 'command', stdout: beat.stdout, stderr: '', exit_code: 0, duration: beat.duration ?? 0.5},
    },
  });
}

function tick() {
  if (paused) return;
  if (storyIndex < STATIC_EVENTS.length) {
    broadcast([revealStatic(STATIC_EVENTS[storyIndex])]);
    storyIndex += 1;
  } else {
    synthesizeRound8Pair();
  }
}

function schedule() {
  const delay = storyIndex < STATIC_EVENTS.length ? 600 + Math.random() * 600 : 3000 + Math.random() * 3000;
  setTimeout(() => {
    tick();
    schedule();
  }, delay);
}

let clockStarted = false;
function startClock() {
  if (clockStarted) return;
  clockStarted = true;
  revealBurst();
  schedule();
}

function currentStatus() {
  let status = 'starting';
  for (const event of timeline) {
    if (event.data?.kind === 'run_status_changed') status = event.data.status;
    if (event.type === 'run_finished') status = 'completed';
    if (event.type === 'run_failed' || event.type === 'run_interrupted') status = 'failed';
  }
  return status;
}

/** Highest round any revealed event names, so queries reveal rounds as they start. */
function currentRound() {
  let round = 0;
  for (const event of timeline) {
    const match = /^round-(\d+)/.exec(event.round_label ?? '');
    if (match) round = Math.max(round, Number(match[1]));
  }
  return round;
}

// query.performance answers with one row per round that recorded a value, the same
// shape capture.mjs's mock gateway uses (see clients/web/scripts/capture.mjs).
function perfRounds(experiments) {
  return experiments
    .flatMap(entry => entry.rounds ?? [])
    .filter(round => typeof round.perf_metric === 'number')
    .map(round => ({
      round: round.round,
      perf_metric: round.perf_metric,
      perf_unit: round.perf_unit ?? '',
      passed: round.passed !== false,
    }));
}

// --- run controls: pause/resume flip status through the same timeline, and --
// a steer is queued, then "consumed" by the round currently in progress, the
// way the real gateway acknowledges a command with a status transition
// rather than folding it into the ack itself (see session.ts's #onMessage).
function handlePause() {
  if (paused || currentStatus() === 'paused') return;
  emit({type: 'run_status_changed', data: {kind: 'run_status_changed', status: 'pausing', previous: 'running'}});
  if (pauseTimer) clearTimeout(pauseTimer);
  pauseTimer = setTimeout(() => {
    paused = true;
    pauseTimer = null;
    emit({type: 'run_status_changed', data: {kind: 'run_status_changed', status: 'paused', previous: 'pausing'}});
  }, 1200);
}

function handleResume() {
  if (pauseTimer) {
    clearTimeout(pauseTimer);
    pauseTimer = null;
  }
  const was = currentStatus();
  if (!paused && was !== 'pausing') return;
  paused = false;
  emit({type: 'run_status_changed', data: {kind: 'run_status_changed', status: 'running', previous: was}});
}

function handleSteer(steerText) {
  emit({type: 'control', text: `/steer: ${steerText}`, status: 'pending'});
  // A paused run starts no agent call, so it consumes nothing until it resumes.
  const consume = () => {
    if (paused || pauseTimer) return setTimeout(consume, 500);
    emit({type: 'control', text: '/steer', status: 'consumed', round_label: roundLabel, agent_kind: agentKind});
  };
  setTimeout(consume, 2000 + Math.random() * 1500);
}

function handleRequest(request) {
  const upTo = currentRound();
  if (request.type === 'query.snapshot') {
    return {
      snapshot: {
        run_id: RUN_ID,
        sequence: timeline.at(-1)?.sequence ?? 0,
        status: currentStatus(),
        agent_kind: agentKind,
        round_label: roundLabel,
        active_executions: [],
      },
    };
  }
  if (request.type === 'query.experiments') {
    return {
      experiments_ready: true,
      experiments: EXPERIMENTS.filter(entry => entry.first_round <= upTo),
    };
  }
  if (request.type === 'query.design') {
    return {design_ready: true, design: DESIGN.filter(round => round.round <= upTo)};
  }
  if (request.type === 'query.performance') {
    const visible = EXPERIMENTS.filter(entry => entry.first_round <= upTo);
    return {performance: perfRounds(visible), performance_context: CONTEXT};
  }
  if (request.type === 'query.events') {
    return {
      events: timeline.filter(
        event =>
          event.sequence > request.after_sequence &&
          (request.before_sequence == null || event.sequence < request.before_sequence),
      ),
    };
  }
  if (request.type === 'command.pause') {
    handlePause();
    return {ack: {action: 'pause', status: 'pending'}};
  }
  if (request.type === 'command.resume') {
    handleResume();
    return {ack: {action: 'resume', status: 'pending'}};
  }
  if (request.type === 'command.steer') {
    handleSteer(request.text ?? '');
    return {ack: {action: 'steer', status: 'pending'}};
  }
  return {};
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
};

async function serveStatic(pathname) {
  const relative = pathname === '/' ? 'index.html' : pathname.replace(/^\//, '');
  let file = Bun.file(join(dist, relative));
  if (!(await file.exists())) file = Bun.file(join(dist, 'index.html'));
  const ext = relative.slice(relative.lastIndexOf('.'));
  return new Response(file, {headers: {'Content-Type': MIME[ext] ?? 'application/octet-stream'}});
}

const server = Bun.serve({
  hostname: '127.0.0.1',
  port: Number(process.env.PORT) || 5173,
  async fetch(request, server) {
    const url = new URL(request.url);
    if (url.pathname === '/api/events') {
      return server.upgrade(request) ? undefined : new Response('Upgrade failed', {status: 400});
    }
    if (url.pathname === '/api/request' && request.method === 'POST') {
      const body = await request.json();
      const fields = handleRequest(body);
      return Response.json({protocol_version: 1, request_id: body.request_id, ok: true, ...fields});
    }
    return serveStatic(url.pathname);
  },
  websocket: {
    open() {},
    message(ws, raw) {
      const request = JSON.parse(raw);
      sockets.add(ws);
      startClock();
      ws.send(
        JSON.stringify({
          type: 'subscribed',
          request_id: request.request_id,
          run_id: RUN_ID,
          latest_sequence: timeline.at(-1)?.sequence ?? 0,
        }),
      );
      ws.send(
        JSON.stringify({
          type: 'event_batch',
          events: timeline,
          through_sequence: timeline.at(-1)?.sequence ?? 0,
          history_after_sequence: 0,
        }),
      );
    },
    close(ws) {
      sockets.delete(ws);
    },
  },
});

console.log(`Demo running at http://${server.hostname}:${server.port}`);
console.log('Ctrl+C to stop.');
