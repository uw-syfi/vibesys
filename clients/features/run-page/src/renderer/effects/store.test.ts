import {expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import type {LogReadInput, LogReadOutcome, LogTransport} from '@vibesys/logs';
import {initialCoreState, reduceEvent} from '@vibesys/run-state';
import type {AbortSignalLike} from '@vibesys/transport';
import {headerView, quotaBannerView, stopBannerView} from '../../state/index.js';
import type {LiveSession, SessionObserver} from '../index.js';
import {createRunPageStore, type RunPagePorts} from '../index.js';

/** A clock the test advances by hand: the shell's only source of time. */
class FakeClock {
  readonly #timers = new Set<{at: number; run: () => void}>();
  #now = 0;

  readonly schedule = (run: () => void, delayMs: number): (() => void) => {
    const timer = {at: this.#now + delayMs, run};
    this.#timers.add(timer);
    return () => this.#timers.delete(timer);
  };

  advance(ms: number): void {
    this.#now += ms;
    for (const timer of [...this.#timers]) {
      if (timer.at > this.#now) continue;
      this.#timers.delete(timer);
      timer.run();
    }
  }

  get pending(): number {
    return this.#timers.size;
  }
}

function running(): HostRun {
  return {
    id: 'a1b2c3d4e5f6',
    projectRoot: '~/vibesys-projects/queue-rs',
    runId: '20261015-163047',
    status: 'running',
    phase: 'running',
    paused: null,
    end: null,
    startedAt: 1,
    reason: null,
  };
}

/** The app's side, recording what the store asked; its one session is held open for the test. */
class FakeApp {
  readonly asked: string[] = [];
  observer: SessionObserver | null = null;
  refuse: string | null = null;

  session(): LiveSession {
    return {
      command: command => {
        // Resume now reads as `resume`; a Resume when capacity returns names its `when`.
        this.asked.push(
          command.kind === 'resume' && command.when !== 'now'
            ? `resume:${command.when}`
            : command.kind,
        );
        return Promise.resolve(this.refuse);
      },
      close: () => {
        this.asked.push('close');
        this.observer = null;
      },
    };
  }
}

function fakePorts(stops: boolean[], app = new FakeApp()): RunPagePorts {
  const ignore = (): void => {};
  return {
    stop: force => {
      stops.push(force);
      return Promise.resolve(true);
    },
    attach: instance => {
      app.asked.push(`attach ${instance}`);
      return Promise.resolve(null);
    },
    release: () => app.asked.push('release'),
    openSession: observer => {
      app.asked.push('open');
      app.observer = observer;
      return app.session();
    },
    readHistory: () => Promise.resolve({kind: 'unsupported'}),
    resumeEnded: ignore,
    openWindow: ignore,
    archive: ignore,
    runAgain: ignore,
    logs: null,
  };
}

interface PendingRead {
  readonly input: LogReadInput;
  readonly signal: AbortSignalLike;
}

class FakeLogTransport implements LogTransport {
  readonly reads: PendingRead[] = [];

  read(input: LogReadInput, signal: AbortSignalLike): Promise<LogReadOutcome> {
    this.reads.push({input, signal});
    return new Promise(() => {});
  }
}

async function settled(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
}

const SEED = {
  identity: {host: 'ssh:chelan4', runId: '20261015-163047'},
  title: {project: '~/vibesys-projects/queue-rs', task: 'mpmc'},
  hostLabel: 'chelan4',
  unavailable: {logs: 'not here'},
} as const;

test('force stop is offered after two minutes of stopping on the injected clock, not before', () => {
  const clock = new FakeClock();
  const stops: boolean[] = [];
  const store = createRunPageStore(SEED, fakePorts(stops), clock.schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  store.dispatch({type: 'ask-stop'});
  store.dispatch({type: 'confirm-stop'});
  expect(stops).toEqual([false]);
  clock.advance(2 * 60 * 1000 - 1);
  expect(stopBannerView(store.getState())).toMatchObject({forceOffered: false});
  clock.advance(1);
  expect(stopBannerView(store.getState())).toMatchObject({forceOffered: true});
  store.dispatch({type: 'force-stop'});
  expect(stops).toEqual([false, true]);
  store.dispose();
});

test('dispose cancels an armed bound', () => {
  const clock = new FakeClock();
  const store = createRunPageStore(SEED, fakePorts([]), clock.schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: {...running(), phase: 'stopping'}}});
  expect(clock.pending).toBe(1);
  store.dispose();
  expect(clock.pending).toBe(0);
});

test('an instance replacement restarts a follower even when the stream id is reused', async () => {
  const clock = new FakeClock();
  const transport = new FakeLogTransport();
  const stream: LogStream = {
    id: 'startup',
    owner: {kind: 'startup_step', stage: 'building_image'},
    command: 'docker build .',
    retained_start: 0,
    size: 0,
    outcome: null,
  };
  const ports: RunPagePorts = {
    ...fakePorts([]),
    logs: {
      list: instanceId => Promise.resolve({ok: true, instanceId, streams: [stream]}),
      transport,
    },
  };
  const store = createRunPageStore({...SEED, unavailable: {}}, ports, clock.schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: {...running(), phase: 'starting'}}});
  await settled();
  expect(transport.reads.map(read => read.input.instance_id)).toEqual(['a1b2c3d4e5f6']);

  store.dispatch({
    type: 'record',
    record: {kind: 'live', run: {...running(), id: 'fedcba654321', phase: 'starting'}},
  });
  await settled();

  expect(transport.reads[0]?.signal.aborted).toBe(true);
  expect(transport.reads.map(read => read.input.instance_id)).toEqual([
    'a1b2c3d4e5f6',
    'fedcba654321',
  ]);
  store.dispose();
});

const LIVE_VIEW = {hasSnapshot: true, streaming: true, controls: true, error: null};

test('a live run is attached, its session opened, and Pause and Resume go through it', async () => {
  const app = new FakeApp();
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  await Promise.resolve();
  expect(app.asked).toEqual(['attach a1b2c3d4e5f6', 'open']);
  expect(headerView(store.getState()).pause.disabledReason).toBe('Connecting to the run…');
  app.observer?.session(LIVE_VIEW);
  expect(headerView(store.getState()).pause.disabledReason).toBeNull();
  store.dispatch({type: 'pause'});
  app.refuse = 'not now';
  store.dispatch({type: 'pause'});
  await Promise.resolve();
  expect(app.asked.slice(2)).toEqual(['pause', 'pause']);
  expect(store.getState().notice).toBe('not now');
  // The run ends: its session closes and the window lets it go.
  store.dispatch({
    type: 'record',
    record: {kind: 'ended', run: {...running(), phase: 'completed'}},
  });
  expect(app.asked.slice(4)).toEqual(['close', 'release']);
  store.dispose();
});

test('a server that refuses to wait for quota no longer offers the wait for that run', async () => {
  const app = new FakeApp();
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  const paused: HostRun = {...running(), status: 'paused', phase: 'paused', paused: 'quota'};
  store.dispatch({type: 'record', record: {kind: 'live', run: paused}});
  await Promise.resolve();
  app.observer?.session(LIVE_VIEW);
  const event = {
    sequence: 1,
    type: 'quota_paused',
    data: {kind: 'quota_paused', provider: 'claude', condition: 'quota_exhausted', detail: 'limit'},
  } as unknown as RunEvent;
  app.observer?.core(reduceEvent(initialCoreState(), event));
  expect(quotaBannerView(store.getState(), String)?.wait.shown).toBe(true);
  // An older server rejects the `when` field.
  app.refuse = "unknown field 'when'";
  store.dispatch({type: 'wait-for-quota'});
  await settled();
  expect(app.asked.slice(2)).toEqual(['resume:capacity_returns']);
  expect(quotaBannerView(store.getState(), String)?.wait.shown).toBe(false);
  expect(store.getState().notice).toBe("The run cannot wait for its quota: unknown field 'when'");
  store.dispatch({type: 'wait-for-quota'});
  await settled();
  expect(app.asked.slice(2)).toEqual(['resume:capacity_returns']);
  // Resume now still goes to the run.
  app.refuse = null;
  store.dispatch({type: 'resume'});
  await settled();
  expect(app.asked.slice(2)).toEqual(['resume:capacity_returns', 'resume']);
  store.dispose();
});

test('a session that reports while it opens is opened once', async () => {
  const app = new FakeApp();
  const ports = fakePorts([], app);
  const store = createRunPageStore(
    SEED,
    {
      ...ports,
      openSession: observer => {
        const session = ports.openSession(observer);
        // The real session reports its state at once, which reconciles the store again.
        observer.session(LIVE_VIEW);
        return session;
      },
    },
    new FakeClock().schedule,
  );
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  await Promise.resolve();
  expect(app.asked.filter(asked => asked === 'open')).toHaveLength(1);
  expect(store.getState().session).toEqual(LIVE_VIEW);
  store.dispose();
});

test('a session that reports after its run ended reaches nothing', async () => {
  const app = new FakeApp();
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  await Promise.resolve();
  const late = app.observer;
  store.dispatch({type: 'record', record: {kind: 'unlisted'}});
  late?.session(LIVE_VIEW);
  expect(store.getState().session).toBeNull();
  store.dispose();
});
