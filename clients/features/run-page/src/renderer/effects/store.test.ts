import {expect, test} from 'bun:test';
import type {RunSnapshot} from '@vibesys/contracts';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import type {LogReadInput, LogReadOutcome, LogTransport} from '@vibesys/logs';
import type {AbortSignalLike} from '@vibesys/transport';
import fc from 'fast-check';
import {
  ageWords,
  clockAt,
  createRunReader,
  headerView,
  nowView,
  pauseNowBannerView,
  type ReaderReply,
  type RunPageAction,
  runNow,
} from '../../state/index.js';
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
  /** What attaching the reader of an ended run comes to. */
  readerReply: ReaderReply = {kind: 'attached'};
  /** When set, a command fails to reach the run (its promise rejects) with this message. */
  fail: string | null = null;
  /** This window's clock, which the test advances by hand. */
  wallMs = 0;
  resumeReply: 'accepted' | 'failed' | 'pending' = 'accepted';
  finishResume: (() => void) | undefined;

  resume(): Promise<void> {
    this.asked.push('resume');
    if (this.resumeReply === 'failed') return Promise.reject(new Error('Host unavailable'));
    if (this.resumeReply === 'accepted') return Promise.resolve();
    return new Promise(resolve => {
      this.finishResume = resolve;
    });
  }

  session(): LiveSession {
    return {
      chat: {
        ask: () => {
          this.asked.push('chat:ask');
          return Promise.resolve({ok: true});
        },
        createThread: () => {
          this.asked.push('chat:createThread');
          return Promise.resolve({ok: true, threadId: 'thread-1'});
        },
        control: () => {
          this.asked.push('chat:control');
          return Promise.resolve({kind: 'acknowledged'});
        },
      },
      command: command => {
        this.asked.push(command.kind);
        return this.fail === null
          ? Promise.resolve(this.refuse)
          : Promise.reject(new Error(this.fail));
      },
      close: () => {
        this.asked.push('close');
        this.observer = null;
      },
    };
  }
}

function fakePorts(_stops: boolean[], app = new FakeApp()): RunPagePorts {
  const ignore = (): void => {};
  return {
    attach: instance => {
      app.asked.push(`attach ${instance}`);
      return Promise.resolve(null);
    },
    release: () => app.asked.push('release'),
    attachReader: () => {
      app.asked.push('attach-reader');
      return Promise.resolve(app.readerReply);
    },
    openSession: observer => {
      app.asked.push('open');
      app.observer = observer;
      return app.session();
    },
    readHistory: () => Promise.resolve({kind: 'unsupported'}),
    readTrace: () => Promise.resolve({kind: 'unsupported'}),
    readSummary: () => Promise.resolve({kind: 'unsupported'}),
    readSpan: () => Promise.resolve({kind: 'unsupported'}),
    resumeEnded: () => app.resume(),
    openWindow: ignore,
    now: () => app.wallMs,
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
  identity: {host: 'ssh:host-b', runId: '20261015-163047'},
  title: {project: '~/vibesys-projects/queue-rs', task: 'mpmc'},
  hostLabel: 'host-b',
  unavailable: {logs: 'not here'},
} as const;

test('a stalled historical summary and history expose a retry instead of loading forever', () => {
  const clock = new FakeClock();
  const ports: RunPagePorts = {
    ...fakePorts([]),
    readSummary: () => new Promise(() => {}),
    readHistory: () => new Promise(() => {}),
  };
  const store = createRunPageStore(SEED, ports, clock.schedule);
  store.dispatch({type: 'record', record: {kind: 'historical', status: null}});
  expect(store.getState().summary.kind).toBe('reading');
  clock.advance(30_000);
  expect(store.getState().history.kind).toBe('reading');
  clock.advance(30_000);
  expect(store.getState().history.kind).toBe('failed');
  store.dispatch({type: 'retry-load'});
  expect(store.getState().summary.kind).toBe('reading');
  store.dispose();
  expect(clock.pending).toBe(0);
});

test('dispose cancels an armed bound', () => {
  const clock = new FakeClock();
  const store = createRunPageStore(SEED, fakePorts([]), clock.schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: {...running(), phase: 'stopping'}}});
  expect(clock.pending).toBe(1); // The initial summary read.
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

const ENDED = {kind: 'historical', status: null} as const;

test('chat on an ended run attaches its reader while Chat is open, and lets it go when closed', async () => {
  const app = new FakeApp();
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  const signal = new AbortController().signal;
  store.dispatch({type: 'record', record: ENDED});
  expect(app.asked).toEqual([]);

  store.dispatch({type: 'chat', open: true});
  await settled();
  expect(app.asked).toEqual(['attach-reader', 'open']);
  await expect(store.chat.ask({threadId: 't', text: 'why?'}, signal)).resolves.toEqual({ok: true});

  store.dispatch({type: 'chat', open: false});
  expect(app.asked.slice(3)).toEqual(['close', 'release']);
  await expect(store.chat.ask({threadId: 't', text: 'why?'}, signal)).resolves.toMatchObject({
    ok: false,
  });
  store.dispose();
});

test('Chat opened on a live run attaches no reader', async () => {
  const app = new FakeApp();
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  store.dispatch({type: 'chat', open: true});
  await settled();
  expect(app.asked).toEqual(['attach a1b2c3d4e5f6', 'open']);
  store.dispose();
});

test('a host that cannot serve an ended run leaves Chat off and is not asked again', async () => {
  const app = new FakeApp();
  app.readerReply = {kind: 'unsupported'};
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  store.dispatch({type: 'record', record: ENDED});
  store.dispatch({type: 'chat', open: true});
  await settled();
  store.dispatch({type: 'chat', open: false});
  store.dispatch({type: 'chat', open: true});
  await settled();
  expect(store.getState().reader.link).toEqual({kind: 'unsupported'});
  expect(app.asked).toEqual(['attach-reader']);
  store.dispose();
});

test('a run that goes again lets the reader go before its live server is attached', async () => {
  const app = new FakeApp();
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  store.dispatch({type: 'record', record: ENDED});
  store.dispatch({type: 'chat', open: true});
  await settled();
  app.asked.length = 0;
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  await settled();
  expect(app.asked).toEqual(['close', 'release', 'attach a1b2c3d4e5f6', 'open']);
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

test('a command that fails to reach the run is shown as a notice, and the next command clears it', async () => {
  const app = new FakeApp();
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  await Promise.resolve();
  app.observer?.session(LIVE_VIEW);
  app.fail = 'the run does not answer command.pause';
  store.dispatch({type: 'pause'});
  await settled();
  expect(store.getState().notice).toBe('the run does not answer command.pause');
  app.fail = null;
  store.dispatch({type: 'pause'});
  expect(store.getState().notice).toBeNull();
  store.dispose();
});

test('Pause now stays in the header, unavailable, while its confirmation is open', async () => {
  const store = createRunPageStore(SEED, fakePorts([]), new FakeClock().schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  await settled();
  store.dispatch({
    type: 'session',
    session: {hasSnapshot: true, streaming: true, controls: true, error: null},
  });
  expect(headerView(store.getState()).pauseNow).toEqual({shown: true, disabledReason: null});
  store.dispatch({type: 'ask-pause-now'});
  expect(pauseNowBannerView(store.getState()).kind).toBe('confirm');
  expect(headerView(store.getState()).pauseNow.shown).toBe(true);
  expect(headerView(store.getState()).pauseNow.disabledReason).not.toBeNull();
  store.dispatch({type: 'cancel-pause-now'});
  expect(headerView(store.getState()).pauseNow.disabledReason).toBeNull();
  store.dispose();
});

for (const outcome of ['refused', 'failed'] as const) {
  test(`regression: Pause now ${outcome} releases confirmation and permits a retry`, async () => {
    const app = new FakeApp();
    const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
    store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
    await settled();
    app.observer?.session(LIVE_VIEW);
    if (outcome === 'failed') app.fail = 'unavailable';
    else app.refuse = 'unavailable';
    store.dispatch({type: 'ask-pause-now'});
    store.dispatch({type: 'confirm-pause-now'});
    expect(headerView(store.getState()).pauseNow.disabledReason).toBe('Pausing');
    store.dispatch({type: 'ask-pause-now'});
    store.dispatch({type: 'confirm-pause-now'});
    await settled();
    expect(app.asked.filter(command => command === 'pause-now')).toHaveLength(1);
    expect(store.getState().notice).toBe('unavailable');
    expect(store.getState().pauseNow.kind).toBe('idle');
    expect(headerView(store.getState()).pauseNow.disabledReason).toBeNull();
    app.fail = null;
    app.refuse = null;
    store.dispatch({type: 'ask-pause-now'});
    store.dispatch({type: 'confirm-pause-now'});
    await settled();
    expect(app.asked.filter(command => command === 'pause-now')).toHaveLength(2);
    expect(store.getState().pauseNow.kind).toBe('sent');
    store.dispose();
  });
}

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

/** A running multi-agent run: its root, a round (workstream) and the orchestrator's turn under it. */
function snapshotWith(openSince: number): RunSnapshot {
  const base = {
    status: 'running' as const,
    waiting_on: [],
    attempts: [],
    ended_at: null,
    failure: null,
    stop: null,
  };
  return {
    run_id: '20261015-163047',
    sequence: 3,
    status: 'running',
    spans: [
      {...base, id: 'flow:#1', parent: null, type: 'flow', name: 'multi', started_at: 0},
      {
        ...base,
        id: 'flow:round#1',
        parent: 'flow:#1',
        type: 'flow',
        name: 'play_round',
        started_at: openSince,
        label: {kind: 'workstream', title: 'round 1', attrs_json: '{"round":1}', links: []},
      },
      {
        ...base,
        id: 'op:round/orchestrator.1',
        parent: 'flow:round#1',
        type: 'operation',
        name: 'agent.turn',
        started_at: openSince,
        label: {
          kind: 'agent_turn',
          title: 'orchestrator',
          attrs_json: '{"role":"orchestrator"}',
          links: [],
        },
      },
    ],
  } as RunSnapshot;
}

function liveStore(app: FakeApp, clock: FakeClock) {
  const store = createRunPageStore(SEED, fakePorts([], app), clock.schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  return store;
}

test('an open step keeps aging while the run says nothing, on the window clock', async () => {
  const app = new FakeApp();
  const clock = new FakeClock();
  const store = liveStore(app, clock);
  await Promise.resolve();
  app.wallMs = 1_000_000;
  app.observer?.session(LIVE_VIEW);
  app.observer?.data({kind: 'snapshot', snapshot: snapshotWith(10)});
  // How long the step Now shows has run, as its row reads it.
  const age = (): string | undefined => {
    const state = store.getState();
    const model = createRunReader().read(state.run.trace, null);
    const [item] = nowView(model);
    return item === undefined ? undefined : ageWords(item.since, clockAt(model, runNow(state)));
  };
  expect(age()).toBe('0s');
  // Ten minutes pass with no event from the run.
  app.wallMs += 10 * 60_000;
  clock.advance(1000);
  expect(age()).toBe('10m');
  store.dispose();
});

test('time is carried while an attached run is at its live end, whatever the screen shows', async () => {
  const app = new FakeApp();
  const clock = new FakeClock();
  const store = liveStore(app, clock);
  await Promise.resolve();
  app.wallMs = 1_000_000;
  app.observer?.session(LIVE_VIEW);
  app.observer?.data({kind: 'snapshot', snapshot: snapshotWith(10)});
  expect(clock.pending).toBe(1);
  const start = runNow(store.getState()) ?? 0;
  // Every action that shows a log once stopped the clock.
  const faces: RunPageAction[] = [{type: 'view-logs'}, {type: 'select-stream', streamId: 's1'}];
  for (const [index, action] of faces.entries()) {
    store.dispatch(action);
    expect(clock.pending).toBe(1);
    app.wallMs += 60_000;
    clock.advance(1000);
    expect(runNow(store.getState())).toBe(start + 60 * (index + 1));
  }
  store.dispatch({type: 'view', view: {at: 3}});
  expect(clock.pending).toBe(0);
  expect(runNow(store.getState())).toBeNull();
  store.dispatch({type: 'view', view: {at: 'live'}});
  store.dispose();
  expect(clock.pending).toBe(0);
});

test('a session that reports after its run ended reaches nothing', async () => {
  const app = new FakeApp();
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  await Promise.resolve();
  const late = app.observer;
  store.dispatch({type: 'record', record: {kind: 'unlisted', why: 'machine'}});
  late?.session(LIVE_VIEW);
  expect(store.getState().session).toBeNull();
  store.dispose();
});

test('chat reaches the run only through its attached session', async () => {
  const app = new FakeApp();
  const store = createRunPageStore(SEED, fakePorts([], app), new FakeClock().schedule);
  const signal = new AbortController().signal;
  await expect(store.chat.ask({threadId: 't', text: 'why?'}, signal)).resolves.toEqual({
    ok: false,
    message: 'the run is not connected',
  });
  await expect(store.chat.control({control_id: 'c', action: 'pause'})).resolves.toEqual({
    kind: 'rejected',
    message: 'the run is not connected',
  });
  store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
  await Promise.resolve();
  await expect(store.chat.ask({threadId: 't', text: 'why?'}, signal)).resolves.toEqual({ok: true});
  await expect(store.chat.createThread(null, signal)).resolves.toEqual({
    ok: true,
    threadId: 'thread-1',
  });
  await expect(store.chat.control({control_id: 'c', action: 'pause'})).resolves.toEqual({
    kind: 'acknowledged',
  });
  expect(app.asked.filter(each => each.startsWith('chat:'))).toEqual([
    'chat:ask',
    'chat:createThread',
    'chat:control',
  ]);
  store.dispose();
});

test('property: legacy Resume immediately reports progress, bounds unanswered requests, reports refusals and ignores late replies', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.constantFrom('completed', 'failed', 'stopped'),
      fc.boolean(),
      fc.constantFrom('accepted', 'failed', 'pending'),
      fc.integer({min: 1, max: 34999}),
      async (status, resumable, reply, elapsed) => {
        const clock = new FakeClock();
        const app = new FakeApp();
        app.resumeReply = reply;
        const store = createRunPageStore(SEED, fakePorts([], app), clock.schedule);
        const run: HostRun = {
          ...running(),
          status,
          phase: status,
          end: {outcome: status === 'completed' ? 'adopted' : status, reason: '', resumable},
        };
        try {
          store.dispatch({type: 'record', record: {kind: 'ended', run}});
          store.dispatch({type: 'resume-ended'});
          expect(store.getState().resumeStatus?.pending).toBe(true);
          expect(store.getState().resumeStatus?.message).toContain('Waiting for the run');
          store.dispatch({type: 'resume-ended'});
          expect(app.asked.filter(each => each === 'resume')).toHaveLength(1);
          await settled();
          if (reply === 'failed') {
            expect(store.getState().resumeStatus?.message).toContain('Host unavailable');
            expect(store.getState().resumeStatus?.pending).toBe(false);
          } else {
            clock.advance(elapsed);
            store.dispatch({type: 'record', record: {kind: 'ended', run}});
            expect(store.getState().resumeStatus?.pending).toBe(true);
            clock.advance(35000 - elapsed);
            expect(store.getState().resumeStatus?.message).toContain('not confirmed');
            app.finishResume?.();
            await settled();
            expect(store.getState().resumeStatus?.message).toContain('not confirmed');
          }
          store.dispatch({type: 'record', record: {kind: 'live', run: running()}});
          expect(store.getState().resumeStatus).toBeNull();
        } finally {
          store.dispose();
        }
        expect(clock.pending).toBe(0);
      },
    ),
    {numRuns: 24, seed: 34365},
  );
});
