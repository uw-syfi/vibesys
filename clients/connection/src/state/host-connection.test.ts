import {describe, expect, test} from 'bun:test';
import {clockFrom} from '@vibesys/machine';
import {checkMachine, type Live, type MachineAction} from '@vibesys/machine/testing';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {createActor, fromPromise, initialTransition, transition} from 'xstate';
import {
  type Cause,
  type ConnectionConfig,
  type ConnectionEvent,
  connectionMachine,
  invariants,
  type Opened,
  type Phase,
  phaseOf,
  type RunUnreachable,
} from './host-connection.js';

const FIRST_DELAY = 500;
const CONFIG: ConnectionConfig = {
  delaysMs: [FIRST_DELAY, 1_000, 2_000],
  deadlineMs: 20_000,
  heartbeatMs: 15_000,
};
const CAUSES: readonly Cause[] = [
  'link',
  'link',
  'link',
  'auth',
  'incompatible',
  'run-gone',
  'denied',
];
const RUN = 'abcdefabcdef';
/** What a handshake or a check that opened a control channel returns: opaque to the machine. */
const OPENED = {channel: 'opened'};

type Snapshot = ReturnType<typeof connectionMachine.getInitialSnapshot>;
type Purpose = 'deadline' | 'heartbeat' | 'backoff';

/** The type of each action, in order: actions carry the actor scope, too large to compare. */
function types(actions: readonly MachineAction[]): string[] {
  return actions.map(action => action.type);
}

function emitted(actions: readonly MachineAction[]): RunUnreachable[] {
  return actions.flatMap(action =>
    action.type === 'xstate.emit' ? [(action.params as {event: RunUnreachable}).event] : [],
  );
}

/**
 * The machine driven through the pure `transition`, with what the live actor would hold: the
 * operation running (the snapshot's child) and the delays armed, by id.
 */
class Drive {
  snapshot: Snapshot;
  /** Every action of the last step. */
  actions: readonly MachineAction[] = [];
  readonly #timers = new Map<string, {readonly type: string}>();

  constructor(config = CONFIG) {
    const [snapshot, actions] = initialTransition(connectionMachine, {config});
    this.snapshot = snapshot;
    this.#apply(actions as readonly MachineAction[]);
  }

  get phase(): Phase {
    return phaseOf(this.snapshot);
  }

  /** The operation running, or null. */
  get operation(): string | null {
    return Object.keys(this.snapshot.children)[0] ?? null;
  }

  /** The purpose of each armed delay. */
  get delays(): Purpose[] {
    return [...this.#timers.keys()].map(id => id.split('.')[2] as Purpose);
  }

  send(event: {readonly type: string}): this {
    const [snapshot, actions] = transition(connectionMachine, this.snapshot, event as never);
    this.#timers.delete(event.type);
    this.snapshot = snapshot;
    this.#apply(actions as readonly MachineAction[]);
    return this;
  }

  event(event: ConnectionEvent): this {
    return this.send(event);
  }

  succeed(output: unknown = OPENED): this {
    return this.send({type: `xstate.done.actor.${this.#live()}`, output} as {type: string});
  }

  fail(cause: Cause, detail = 'why'): this {
    const error = {reason: cause, message: detail};
    return this.send({type: `xstate.error.actor.${this.#live()}`, error} as {type: string});
  }

  fire(purpose: Purpose): this {
    const id = [...this.#timers.keys()].find(key => key.startsWith(`xstate.after.${purpose}.`));
    if (id === undefined) throw new Error(`no ${purpose} delay armed`);
    return this.send(this.#timers.get(id) as {type: string});
  }

  /** The params of each action of `type` the last step ran. */
  params(type: string): unknown[] {
    return this.actions.flatMap(action => (action.type === type ? [action.params] : []));
  }

  /** The delays the last step armed, in milliseconds. */
  armed(purpose: Purpose): number[] {
    return this.actions.flatMap(action => {
      const params = action.params as {id?: string; delay?: number} | undefined;
      return action.type === 'xstate.raise' &&
        params?.delay !== undefined &&
        params.id?.startsWith(`xstate.after.${purpose}.`) === true
        ? [params.delay]
        : [];
    });
  }

  /** The operations the last step started, with their input. */
  started(): {id: string; input: unknown}[] {
    return this.actions.flatMap(action => {
      const params = action.params as {id?: string; input?: unknown} | undefined;
      return action.type === 'xstate.spawnChild' && params?.id !== undefined
        ? [{id: params.id, input: params.input}]
        : [];
    });
  }

  #live(): string {
    const operation = this.operation;
    if (operation === null) throw new Error('no operation running');
    return operation;
  }

  #apply(actions: readonly MachineAction[]): void {
    this.actions = actions;
    for (const action of actions) {
      const params = (action.params ?? {}) as {
        id?: string;
        delay?: number;
        event?: {type: string};
        sendId?: string;
      };
      if (action.type === 'xstate.raise' && params.delay !== undefined && params.id !== undefined) {
        this.#timers.set(params.id, params.event ?? {type: params.id});
      }
      if (action.type === 'xstate.cancel') this.#timers.delete(params.sendId ?? '');
    }
  }
}

function readyDrive(): Drive {
  return new Drive().event({type: 'open'}).succeed().succeed();
}

function plain(event: ConnectionEvent): {readonly type: string} {
  return event;
}

/** One random event: a plain event, or an outcome of what is live now. */
function randomEvent(random: Seeded, live: Live): {readonly type: string} | null {
  const [operation] = live.operations;
  const [delay] = live.delays;
  switch (random.int(10)) {
    case 0:
      return {type: 'open'};
    case 1:
      return random.int(8) === 0 ? {type: 'close'} : {type: 'wake'};
    case 2:
    case 3:
      return operation === undefined ? null : live.done(operation, random.bool() ? OPENED : null);
    case 4:
      return operation === undefined
        ? null
        : live.error(operation, {reason: random.pick(CAUSES), message: 'why'});
    case 5:
    case 6:
      return delay === undefined ? null : live.fire(delay);
    case 7:
      return plain({type: 'control-ended', cause: random.pick(CAUSES), detail: 'ended'});
    case 8:
      return plain({type: 'run-ended', instanceId: RUN, cause: random.pick(CAUSES), detail: 'x'});
    default:
      return {type: 'user-retry'};
  }
}

/** Only failures and timers: a host that never answers. */
function unanswered(random: Seeded, drive: Drive): void {
  const [delay] = drive.delays;
  if (drive.operation !== null && random.bool()) drive.fail('link', 'down');
  else if (delay !== undefined) drive.fire(delay);
  else drive.event({type: 'wake'});
}

function isStuck(phase: Phase): boolean {
  return phase.kind === 'auth-needed' || phase.kind === 'incompatible';
}

describe('HostConnection machine (model-based)', () => {
  test('work lives only in its state, terminal phases absorb, and attempts fit the schedule', () => {
    checkMachine(
      {
        machine: connectionMachine,
        input: {config: CONFIG},
        // A machine in its final state is done: the live actor takes no more events.
        event: (random, live, snapshot) =>
          snapshot.status === 'done' ? null : randomEvent(random, live),
        invariants: snapshot => invariants(snapshot),
        also: (before, event, after, actions) => {
          const was = phaseOf(before);
          const now = phaseOf(after);
          const retried = event.type === 'user-retry' || event.type === 'close';
          if (isStuck(was) && !retried) expect(now).toEqual(was);
          // A run channel lost to the link leaves the host not ready, so the next ready wakes its dial.
          const lostRun = event as {type: string; cause?: Cause};
          if (lostRun.type === 'run-ended' && lostRun.cause === 'link') {
            expect(now.kind).not.toBe('ready');
          }
          // At most one operation starts per step.
          const starts = actions.filter(action => action.type === 'xstate.spawnChild');
          expect(starts.length).toBeLessThanOrEqual(1);
          // A wake never leaves a stale operation running: whatever was in flight is stopped and a
          // fresh operation of the same kind takes its place at once, across every state that can
          // hold one.
          const stale = Object.keys(before.children)[0];
          if (event.type === 'wake' && stale !== undefined) {
            expect(types(actions)).toContain('xstate.stopChild');
            expect(starts).toHaveLength(1);
            expect(Object.keys(after.children)).toEqual([stale]);
            expect(after.children[stale]).not.toBe(before.children[stale]);
          }
        },
      },
      80,
    );
  });

  test('a run of link failures walks the whole schedule in order, then the host is offline', () => {
    for (const seed of SEEDS.slice(0, 50)) {
      const random = new Seeded(seed);
      const drive = new Drive().event({type: 'open'});
      let attempts = 1;
      const backoffs: number[] = [];
      for (let index = 0; index < 100 && drive.phase.kind !== 'offline'; index += 1) {
        unanswered(random, drive);
        attempts += drive.started().length;
        backoffs.push(...drive.armed('backoff'));
      }
      expect({seed, phase: drive.phase.kind}).toEqual({seed, phase: 'offline'});
      expect({seed, backoffs}).toEqual({seed, backoffs: [...CONFIG.delaysMs]});
      expect(attempts).toBe(CONFIG.delaysMs.length + 1);
    }
  });
});

describe('HostConnection machine (scenarios)', () => {
  test('opens through linking and handshaking to ready with a heartbeat', () => {
    const drive = new Drive().event({type: 'open'});
    expect(drive.started()).toEqual([{id: 'link', input: {interactive: false}}]);
    drive.succeed().succeed();
    expect(drive.phase).toEqual({kind: 'ready'});
    expect(drive.delays).toEqual(['heartbeat']);
    expect(drive.snapshot.context.attempt).toBe(0);
    expect(drive.params('installControl')).toEqual([{opened: OPENED}]);
  });

  test('a run channel lost to the link is checked with a fresh run.connect to that run', () => {
    const drive = readyDrive().event({
      type: 'run-ended',
      instanceId: RUN,
      cause: 'link',
      detail: 'reset',
    });
    expect(drive.phase).toEqual({
      kind: 'reconnecting',
      attempt: 1,
      target: {kind: 'run', instanceId: RUN},
    });
    expect(drive.started()).toEqual([
      {id: 'check', input: {target: {kind: 'run', instanceId: RUN}}},
    ]);
  });

  test('regression: the first failed check after a drop or a wake waits the first slot', () => {
    const drops: ConnectionEvent[] = [
      {type: 'control-ended', cause: 'link', detail: 'reset'},
      {type: 'run-ended', instanceId: RUN, cause: 'link', detail: 'reset'},
    ];
    for (const drop of drops) {
      const drive = readyDrive().event(drop).fail('link', 'down');
      expect({drop: drop.type, delays: drive.armed('backoff')}).toEqual({
        drop: drop.type,
        delays: [FIRST_DELAY],
      });
    }
    // Offline, then a wake: its check failing waits the first slot too.
    const drive = readyDrive().event(drops[0] as ConnectionEvent);
    for (let index = 0; drive.phase.kind !== 'offline' && index < 20; index += 1) {
      if (drive.operation !== null) drive.fail('link', 'down');
      else drive.fire('backoff');
    }
    expect(drive.phase.kind).toBe('offline');
    drive.event({type: 'wake'});
    expect(drive.phase).toMatchObject({kind: 'reconnecting', attempt: 1});
    expect(drive.fail('link', 'down').armed('backoff')).toEqual([FIRST_DELAY]);
  });

  test('a recovery that did not hold backs off instead of checking at once', () => {
    const drive = readyDrive()
      .event({type: 'control-ended', cause: 'link', detail: 'x'})
      .succeed(null);
    expect(drive.phase.kind).toBe('ready');
    drive.event({type: 'control-ended', cause: 'link', detail: 'again'});
    expect(drive.operation).toBeNull();
    expect(drive.armed('backoff')).toEqual([FIRST_DELAY]);
    // A heartbeat that answers proves the connection: the next loss checks at once.
    drive.fire('backoff').succeed(null).fire('heartbeat').succeed().fire('heartbeat').succeed();
    expect(drive.snapshot.context.attempt).toBe(0);
    drive.event({type: 'control-ended', cause: 'link', detail: 'x'});
    expect(drive.operation).toBe('check');
  });

  test('an operation past its deadline is stopped, so it can no longer report', () => {
    const drive = new Drive().event({type: 'open'});
    drive.fire('deadline');
    expect(types(drive.actions)).toContain('xstate.stopChild');
    expect(drive.phase).toMatchObject({kind: 'reconnecting', attempt: 1});
    expect(drive.operation).toBeNull();
  });

  test('regression: a wake while the heartbeat ping is already in flight stops it and pings again', () => {
    const drive = readyDrive().fire('heartbeat');
    const stalePing = drive.snapshot.children['ping'];
    expect(stalePing).toBeDefined();
    drive.event({type: 'wake'});
    expect(types(drive.actions)).toContain('xstate.stopChild');
    expect(drive.phase.kind).toBe('ready');
    expect(drive.started()).toEqual([{id: 'ping', input: undefined}]);
    expect(drive.snapshot.children['ping']).not.toBe(stalePing);
  });

  test('a channel lost under a ping, and closing, stop the operation in flight', () => {
    const drive = readyDrive().fire('heartbeat');
    expect(drive.operation).toBe('ping');
    drive.event({type: 'control-ended', cause: 'link', detail: 'x'});
    expect(types(drive.actions)).toContain('xstate.stopChild');
    expect(drive.operation).toBe('check');
    drive.event({type: 'close'});
    expect(types(drive.actions)).toContain('xstate.stopChild');
    expect(drive.operation).toBeNull();
    expect(drive.delays).toEqual([]);
  });

  test('a run found gone by its check leaves the host ready and reports the run', () => {
    const drive = readyDrive()
      .event({type: 'run-ended', instanceId: RUN, cause: 'link', detail: 'x'})
      .fail('run-gone', 'gone');
    expect(drive.phase.kind).toBe('ready');
    expect(emitted(drive.actions)).toEqual([
      {type: 'run-unreachable', instanceId: RUN, detail: 'gone'},
    ]);
  });

  test('a run dial lost while a recovery is under way is left to that recovery', () => {
    const drive = readyDrive().event({type: 'control-ended', cause: 'link', detail: 'x'});
    const checking = drive.snapshot;
    drive.event({type: 'run-ended', instanceId: RUN, cause: 'link', detail: 'dial'});
    expect(drive.snapshot.value).toEqual(checking.value);
    expect(drive.snapshot.context).toBe(checking.context);
    expect(types(drive.actions)).toEqual([]);
  });

  test('auth stops the loop until the user retries, which links interactively', () => {
    const drive = new Drive().event({type: 'open'}).fail('auth', 'denied');
    expect(drive.phase).toEqual({kind: 'auth-needed', detail: 'denied'});
    drive.event({type: 'wake'});
    expect(drive.phase.kind).toBe('auth-needed');
    drive.event({type: 'user-retry'});
    expect(drive.started()).toEqual([{id: 'link', input: {interactive: true}}]);
  });

  test('closing releases everything once and ends the machine', () => {
    const drive = readyDrive().event({type: 'close'});
    expect(types(drive.actions).filter(type => type === 'release')).toHaveLength(1);
    expect(drive.snapshot.status).toBe('done');
    expect(drive.operation).toBeNull();
    expect(drive.delays).toEqual([]);
  });
});

/** A deferred outcome a live actor's operation waits on. */
function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>(settle => {
    resolve = settle;
  });
  return {promise, resolve};
}

describe('HostConnection machine (live actor)', () => {
  test('a closed connection takes no more events and releases only once', () => {
    let releases = 0;
    const actor = createActor(
      connectionMachine.provide({actions: {release: () => (releases += 1)}}),
      {input: {config: CONFIG}, clock: clockFrom(new FakeClock().scheduleTimeout)},
    );
    actor.start();
    actor.send({type: 'close'});
    for (const event of [{type: 'open'}, {type: 'wake'}, {type: 'close'}] as const) {
      actor.send(event);
    }
    expect(phaseOf(actor.getSnapshot())).toEqual({kind: 'closed'});
    expect(releases).toBe(1);
  });

  test('a handshake that answers after its deadline installs nothing, and its signal was aborted', async () => {
    const clock = new FakeClock();
    const hello = deferred<object>();
    const started = deferred<AbortSignal>();
    const installed: unknown[] = [];
    const actor = createActor(
      connectionMachine.provide({
        actors: {
          link: fromPromise(async () => {}),
          handshake: fromPromise(({signal}) => {
            started.resolve(signal);
            return hello.promise;
          }),
        },
        actions: {installControl: (_, {opened}) => installed.push(opened)},
      }),
      {input: {config: CONFIG}, clock: clockFrom(clock.scheduleTimeout)},
    );
    actor.start();
    actor.send({type: 'open'});
    const signal = await started.promise;
    clock.advanceBy(CONFIG.deadlineMs);
    expect(phaseOf(actor.getSnapshot())).toMatchObject({kind: 'reconnecting', attempt: 1});
    expect(signal.aborted).toBe(true);
    hello.resolve(OPENED);
    await hello.promise;
    await Promise.resolve();
    expect(installed).toEqual([]);
    actor.stop();
  });

  test('an outcome the shell no longer holds is discarded, not installed, and counts as a failure', async () => {
    const discarded: unknown[] = [];
    const installed: unknown[] = [];
    const actor = createActor(
      connectionMachine.provide({
        actors: {
          link: fromPromise(async () => {}),
          handshake: fromPromise<Opened, void>(async () => OPENED),
        },
        actions: {
          installControl: (_, {opened}) => installed.push(opened),
          discard: (_, {opened}) => discarded.push(opened),
        },
        guards: {current: () => false},
      }),
      {input: {config: CONFIG}, clock: clockFrom(new FakeClock().scheduleTimeout)},
    );
    const reconnecting = new Promise<Phase>(resolve => {
      actor.subscribe(snapshot => {
        const phase = phaseOf(snapshot);
        if (phase.kind === 'reconnecting') resolve(phase);
      });
    });
    actor.start();
    actor.send({type: 'open'});
    expect(await reconnecting).toMatchObject({kind: 'reconnecting', attempt: 1});
    expect(discarded).toEqual([OPENED]);
    expect(installed).toEqual([]);
    expect(actor.getSnapshot().context.detail).toContain('ended during the connection operation');
    actor.stop();
  });
});
