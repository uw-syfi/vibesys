import {describe, expect, test} from 'bun:test';
import {type AttachedRun, checkAttachment, observedDial} from './attachment.js';
import {SshHost} from './ssh-host.js';
import {
  type CheckOutcome,
  type ConnectionStatus,
  ConnectionSupervisor,
  INITIAL_STATE,
  isTerminal,
  type SupervisorConfig,
  type SupervisorEvent,
  type SupervisorRequest,
  type SupervisorState,
  step,
} from './supervisor.js';
import {FakeClock} from './testing/fake-clock.js';
import {FakeSsh} from './testing/fake-ssh.js';
import {FakeVibesysNode, fakeRecord} from './testing/fake-vibesys-node.js';

const CONFIG: SupervisorConfig = {
  delaysMs: [500, 1_000, 2_000, 4_000, 8_000],
  checkTimeoutMs: 45_000,
};
const SPENT = CONFIG.delaysMs.reduce((sum, delay) => sum + delay, 0);

/** Let promise callbacks run; a bound on chained microtasks, not a wait on time. */
async function settle(): Promise<void> {
  for (let index = 0; index < 50; index += 1) await Promise.resolve();
}

/** A supervisor over a FakeClock whose checks the test answers. */
function harness(answer: (interactive: boolean) => CheckOutcome | 'hang') {
  const clock = new FakeClock();
  const shown: ConnectionStatus['kind'][] = [];
  const checks: boolean[] = [];
  let wakes = 0;
  const supervisor = new ConnectionSupervisor({
    check: interactive => {
      checks.push(interactive);
      const outcome = answer(interactive);
      return outcome === 'hang' ? new Promise(() => {}) : Promise.resolve(outcome);
    },
    scheduleTimeout: clock.scheduleTimeout,
    wakePage: () => {
      wakes += 1;
    },
    show: status => shown.push(status.kind),
    config: CONFIG,
  });
  return {clock, supervisor, shown, checks, wakes: () => wakes};
}

const OK: CheckOutcome = {ok: true};
const LINK: CheckOutcome = {ok: false, cause: 'link', detail: 'Network is unreachable'};

async function connected(answer: (interactive: boolean) => CheckOutcome | 'hang') {
  let ready = false;
  const world = harness(interactive => (ready ? answer(interactive) : OK));
  world.supervisor.dispatch({type: 'start'});
  await settle();
  expect(world.supervisor.status.kind).toBe('connected');
  ready = true;
  return world;
}

describe('ConnectionSupervisor scenarios', () => {
  test('a drop mid-stream restores the link, then wakes the page once', async () => {
    const world = await connected(() => OK);
    world.supervisor.dispatch({type: 'stream-ended', end: 'link'});
    await settle();
    expect(world.supervisor.status.kind).toBe('connected');
    expect(world.wakes()).toBe(1);
    expect(world.checks).toEqual([true, false]);
  });

  test('a half-open link (a check that never answers) times out into a retry', async () => {
    let calls = 0;
    const world = await connected(() => {
      calls += 1;
      return calls === 1 ? 'hang' : OK;
    });
    world.supervisor.dispatch({type: 'stream-ended', end: 'link'});
    await settle();
    expect(world.supervisor.status.kind).toBe('reconnecting');
    world.clock.advance(CONFIG.checkTimeoutMs);
    await settle();
    expect(world.supervisor.status.kind).toBe('reconnecting');
    world.clock.advance(CONFIG.delaysMs[0] ?? 0);
    await settle();
    expect(world.supervisor.status.kind).toBe('connected');
    expect(world.wakes()).toBe(1);
  });

  test('N refused checks then success follows the backoff and connects', async () => {
    for (let refusals = 1; refusals <= CONFIG.delaysMs.length; refusals += 1) {
      let calls = 0;
      const world = await connected(() => {
        calls += 1;
        return calls <= refusals ? LINK : OK;
      });
      world.supervisor.dispatch({type: 'stream-ended', end: 'link'});
      for (const delay of CONFIG.delaysMs.slice(0, refusals)) {
        await settle();
        expect(world.supervisor.status.kind).toBe('reconnecting');
        world.clock.advance(delay - 1);
        await settle();
        expect(calls).toBeLessThanOrEqual(refusals);
        world.clock.advance(1);
      }
      await settle();
      expect(world.supervisor.status.kind).toBe('connected');
      expect(calls).toBe(refusals + 1);
      expect(world.wakes()).toBe(1);
    }
  });

  test('a link that stays down goes offline instead of retrying forever, until the network changes', async () => {
    let up = false;
    const world = await connected(() => (up ? OK : LINK));
    world.supervisor.dispatch({type: 'stream-ended', end: 'link'});
    await settle();
    for (let index = 0; index < 20; index += 1) {
      world.clock.advance(SPENT);
      await settle();
    }
    expect(world.supervisor.status.kind).toBe('offline');
    expect(world.checks.length).toBe(1 + 1 + CONFIG.delaysMs.length);
    expect(world.clock.pending).toBe(0);
    up = true;
    world.supervisor.dispatch({type: 'network-changed'});
    await settle();
    expect(world.supervisor.status.kind).toBe('connected');
  });

  test('waking from sleep checks at once instead of waiting out the backoff', async () => {
    let up = false;
    const world = await connected(() => (up ? OK : LINK));
    world.supervisor.dispatch({type: 'stream-ended', end: 'link'});
    await settle();
    world.clock.advance(CONFIG.delaysMs[0] ?? 0);
    await settle();
    expect(world.supervisor.status).toEqual({kind: 'reconnecting', attempt: 2});
    // Asleep for eight hours: the pending retry fires on wake, then the wake itself is a no-op.
    up = true;
    world.clock.advance(8 * 3_600_000);
    world.supervisor.dispatch({type: 'resumed'});
    await settle();
    expect(world.supervisor.status.kind).toBe('connected');
    expect(world.wakes()).toBe(1);
  });

  test('authentication required stops retrying until the user retries, and only that prompts', async () => {
    let password = false;
    const world = await connected(interactive =>
      interactive && password ? OK : {ok: false, cause: 'auth', detail: 'Permission denied'},
    );
    world.supervisor.dispatch({type: 'stream-ended', end: 'link'});
    await settle();
    expect(world.supervisor.status.kind).toBe('auth-needed');
    world.clock.advance(10 * SPENT);
    world.supervisor.dispatch({type: 'resumed'});
    world.supervisor.dispatch({type: 'stream-ended', end: 'link'});
    await settle();
    expect(world.checks).toEqual([true, false]);
    password = true;
    world.supervisor.dispatch({type: 'user-retry'});
    await settle();
    expect(world.supervisor.status.kind).toBe('connected');
    expect(world.checks).toEqual([true, false, true]);
  });

  test('a run that is gone ends the attachment with no retry', async () => {
    const world = await connected(() => ({ok: false, cause: 'run-gone', detail: 'gone'}));
    world.supervisor.dispatch({type: 'stream-ended', end: 'run-gone'});
    await settle();
    expect(world.supervisor.status.kind).toBe('run-ended');
    for (let index = 0; index < 5; index += 1) {
      world.supervisor.dispatch({type: 'stream-ended', end: 'run-gone'});
      world.supervisor.dispatch({type: 'network-changed'});
      world.clock.advance(SPENT);
      await settle();
    }
    expect(world.checks.length).toBe(2);
    expect(world.clock.pending).toBe(0);
  });

  test('version skew is shown with its message and not retried', async () => {
    const world = harness(() => ({ok: false, cause: 'version-skew', detail: 'protocol 2 vs 1'}));
    world.supervisor.dispatch({type: 'start'});
    await settle();
    expect(world.supervisor.status).toEqual({kind: 'incompatible', detail: 'protocol 2 vs 1'});
    world.clock.advance(SPENT);
    await settle();
    expect(world.checks.length).toBe(1);
  });
});

describe('supervisor core properties', () => {
  function generator(seed: number): () => number {
    let state = seed >>> 0;
    return () => {
      state = (state * 1_664_525 + 1_013_904_223) >>> 0;
      return state / 2 ** 32;
    };
  }

  const OUTCOMES = ['ok', 'link', 'link', 'auth', 'run-gone', 'version-skew', 'failed'] as const;

  /** A random event the world could deliver now. */
  function nextEvent(
    random: () => number,
    state: SupervisorState,
    timers: ReadonlySet<number>,
  ): SupervisorEvent {
    const pick = <T>(items: readonly T[]): T => items[Math.floor(random() * items.length)] as T;
    const choices: SupervisorEvent[] = [
      {type: 'stream-ended', end: pick(['normal', 'link', 'run-gone', 'failed'] as const)},
      {type: 'resumed'},
      {type: 'network-changed'},
    ];
    if (random() < 0.05) choices.push({type: 'user-retry'});
    if (state.check !== null) {
      const outcome = pick(OUTCOMES);
      choices.push(
        outcome === 'ok'
          ? {type: 'check-succeeded', check: state.check}
          : {type: 'check-failed', check: state.check, cause: outcome, detail: outcome},
        {type: 'check-succeeded', check: state.check - 7},
      );
    }
    for (const timer of timers) choices.push({type: 'timer-fired', timer});
    return pick(choices);
  }

  /** The invariants every reachable state holds. */
  function expectSound(state: SupervisorState, timers: ReadonlySet<number>): void {
    // No orphan waits: a state that is neither settled nor waiting on the user has a producer.
    const settled =
      state.status.kind === 'connected' ||
      state.status.kind === 'offline' ||
      isTerminal(state.status);
    if (!settled) expect(state.check !== null || state.timer !== null).toBe(true);
    // At most one live timer, and it is the one the state names.
    expect(timers.size).toBeLessThanOrEqual(1);
    if (state.timer !== null) expect(timers.has(state.timer)).toBe(true);
  }

  const TRIGGERS: ReadonlySet<SupervisorEvent['type']> = new Set([
    'resumed',
    'network-changed',
    'user-retry',
    'stream-ended',
  ]);

  /** Apply one step's requests to the live timers; return how many checks it started. */
  function observe(
    requests: readonly SupervisorRequest[],
    before: SupervisorState,
    event: SupervisorEvent,
    timers: Set<number>,
  ): number {
    if (event.type === 'timer-fired') timers.delete(event.timer);
    for (const request of requests) {
      if (request.type === 'schedule') timers.add(request.timer);
      else if (request.type === 'cancel') timers.delete(request.timer);
    }
    const checks = requests.filter(request => request.type === 'check').length;
    if (checks > 0) {
      // Never two checks at once: a new one starts only when none is outstanding.
      expect(before.check === null || event.type === 'timer-fired').toBe(true);
      // A status only the user can clear is left only by the user.
      expect(!isTerminal(before.status) || event.type === 'user-retry').toBe(true);
    }
    return checks;
  }

  /** Run a random event sequence; check every invariant after every step. */
  function run(seed: number): SupervisorState {
    const random = generator(seed);
    let state = step(INITIAL_STATE, {type: 'start'}, CONFIG).state;
    const timers = new Set<number>(state.timer === null ? [] : [state.timer]);
    let checksSinceTrigger = 1;
    for (let index = 0; index < 300; index += 1) {
      const event = nextEvent(random, state, timers);
      const next = step(state, event, CONFIG);
      if (TRIGGERS.has(event.type)) checksSinceTrigger = 0;
      checksSinceTrigger += observe(next.requests, state, event, timers);
      // Never a retry storm: one trigger buys at most the finite schedule plus its first try.
      expect(checksSinceTrigger).toBeLessThanOrEqual(CONFIG.delaysMs.length + 1);
      state = next.state;
      expectSound(state, timers);
    }
    return state;
  }

  test('any event sequence keeps one check, a bounded retry count, and a producer for every wait', () => {
    for (let seed = 1; seed <= 300; seed += 1) run(seed);
  });

  test('from any state, a reachable host and the user retrying ends connected', () => {
    for (let seed = 1; seed <= 300; seed += 1) {
      let state = run(seed);
      let next = step(state, {type: 'user-retry'}, CONFIG);
      if (state.check !== null) {
        next = step(state, {type: 'check-succeeded', check: state.check}, CONFIG);
      } else {
        state = next.state;
        next = step(state, {type: 'check-succeeded', check: state.check ?? -1}, CONFIG);
      }
      expect(next.state.status.kind).toBe('connected');
    }
  });
});

describe('supervisor over an SSH host', () => {
  test('a link dropped mid-stream is restored and the page woken; a stopped run ends the attachment', async () => {
    const node = new FakeVibesysNode({
      server: () => connection => connection.pipe(connection),
      command: () => ({code: 0, stdout: '{}', stderr: ''}),
    });
    const ssh = new FakeSsh(node);
    const host = new SshHost({
      alias: 'node-1',
      vibesysCommand: 'vibesys',
      controlPath: '/tmp/vsd/%C',
      askpass: '/app/askpass',
      runner: ssh,
    });
    const server = await host.startServer([]);
    const id = server.record.kind === 'compatible' ? server.record.instance.id : '';
    let live = true;
    // The real listing comes from the node's registry; scripted here from what it started.
    const listing = () => ({
      code: 0,
      stdout: JSON.stringify({
        version: 1,
        instances: live ? [fakeRecord(id, server.endpoint.socketPath)] : [],
      }),
      stderr: '',
    });
    const scripted = new FakeVibesysNode({server: () => () => {}, command: listing});
    const run: AttachedRun = {
      host: {
        ensureLink: () => host.ensureLink(),
        startServer: (args, options) => host.startServer(args, options),
        dial: endpoint => host.dial(endpoint),
        // The command runs over ssh (restoring the master); its answer is the scripted listing.
        invoke: async argv => {
          await host.invoke(argv);
          return JSON.parse(scripted.run(argv, undefined).stdout) as unknown;
        },
        close: () => host.close(),
      },
      hostName: 'node-1',
      vibesysCommand: 'vibesys',
      instanceId: id,
      endpoint: server.endpoint,
    };
    const clock = new FakeClock();
    const checked: Promise<CheckOutcome>[] = [];
    let wakes = 0;
    const supervisor = new ConnectionSupervisor({
      check: interactive => {
        const outcome = checkAttachment(run, interactive);
        checked.push(outcome);
        return outcome;
      },
      scheduleTimeout: clock.scheduleTimeout,
      wakePage: () => {
        wakes += 1;
      },
      show: () => {},
      config: CONFIG,
    });
    supervisor.dispatch({type: 'start'});
    await checked.at(-1);
    await settle();
    expect(supervisor.status.kind).toBe('connected');

    const dial = observedDial(run, end => supervisor.dispatch({type: 'stream-ended', end}));
    const stream = await dial();
    stream.resume();
    const closed = new Promise(resolve => stream.once('close', resolve));
    ssh.dropLink();
    await closed;
    await checked.at(-1);
    await settle();
    expect(supervisor.status.kind).toBe('connected');
    expect(ssh.masterUp).toBe(true);
    expect(wakes).toBe(1);

    live = false;
    node.stopAll();
    await dial().catch(() => {});
    await checked.at(-1);
    await settle();
    expect(supervisor.status.kind).toBe('run-ended');
    await host.close();
  });
});
