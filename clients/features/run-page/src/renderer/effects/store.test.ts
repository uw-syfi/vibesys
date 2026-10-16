import {expect, test} from 'bun:test';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import type {LogReadInput, LogReadOutcome, LogTransport} from '@vibesys/logs';
import type {AbortSignalLike} from '@vibesys/transport';
import {stopBannerView} from '../../state/index.js';
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

function fakePorts(stops: boolean[]): RunPagePorts {
  const ignore = (): void => {};
  return {
    stop: force => {
      stops.push(force);
      return Promise.resolve(true);
    },
    pause: ignore,
    resume: ignore,
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
