/** Wire status -> gateway policy -> main IPC -> preload -> renderer codec -> reader -> screen. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {startGatewayRun} from '@vibesys/feature-run-page/renderer';
import {type LoadView, loadView} from '@vibesys/feature-run-page/state';
import type {GatewayError} from '@vibesys/ipc';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {seedsAtDepth} from '@vibesys/store/testing';
import {gatewayWorld} from '../testing/gateway-world.js';

const statuses: Array<{name: string; code: number; message: string; grpcMessage: string}> =
  JSON.parse(
    readFileSync(
      new URL('../../../../../proto/testdata/grpc_status/statuses.json', import.meta.url),
      'utf8',
    ),
  );
const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'project', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

const RUN_METHODS = ['/Skeleton', '/GetBlobs', '/Tail'];
const BOUNDED_CALLS = 8;
const QUIET_EVENTS = 200;

interface Observation {
  /** What the run screen shows once the world is quiet. */
  readonly screen: LoadView;
  readonly calls: number;
  /** Nothing was left to deliver or fire within the event budget: no retry loop. */
  readonly quiet: boolean;
  /** The renderer-side outcome of one unary call against the same wire status. */
  readonly unary: {code: unknown; message: string} | undefined;
}

/** Drive one wire status through every hop for one scheduler seed. */
async function observe(row: (typeof statuses)[number], seed: number): Promise<Observation> {
  let calls = 0;
  const remote = new FakeRunTransport(storeRunFixture());
  const world = gatewayWorld(seed, call => {
    calls += 1;
    // The host refuses the calls about this run; its other streams (liveness) keep working.
    if (row.code !== 0 && RUN_METHODS.some(name => call.method.endsWith(name))) {
      call.trailers(String(row.code), row.grpcMessage);
      return;
    }
    if (call.method.endsWith('/Skeleton') || call.method.endsWith('/GetBlobs')) {
      void remote.unary(call.method, call.bytes, call.signal).then(bytes => {
        call.message(bytes);
        call.trailers('0', '');
      });
    } else {
      void (async () => {
        for await (const bytes of remote.stream(call.method, call.bytes, call.signal))
          call.message(bytes);
        call.trailers('0', '');
      })();
    }
  });
  const actor = startGatewayRun(
    {
      reader: gatewayReader(world.transport, new FakeSkeletonCache(), world.clock.schedule),
      setting: async () => true,
      watchSetting: () => () => {},
    },
    world.clock.schedule,
    {seed: SEED},
  );
  try {
    // Settled means the world has nothing left to deliver or fire, retries and their timers
    // included; it names no field of the screen, so old and fixed code are observed alike.
    const quiet = await world.schedule.quiesce(QUIET_EVENTS);
    const screen = loadView(actor.getSnapshot().context);
    const screenCalls = calls;
    const outcome = world.bridge.unary(
      'local',
      '/vibesys.api.v1.HostService/Skeleton',
      new Uint8Array([10, 5, 114, 117, 110, 45, 97]),
    );
    const unary = await world.schedule.run(
      outcome.result.then(
        () => undefined,
        (error: GatewayError) => ({code: error.status, message: error.message}),
      ),
    );
    return {
      screen,
      calls: screenCalls,
      quiet,
      unary,
    };
  } finally {
    actor.stop();
    world.close();
  }
}

const SEEDS = seedsAtDepth([1, 7, 23]);
const failing = statuses.filter(row => row.code !== 0);
/** gRPC says retrying the same request cannot change these, so the screen must not call them a dead link. */
const FINAL_CODES = new Set([3, 5, 7, 8, 9, 11, 12, 15, 16]);

// One test per status keeps each below the per-test bound at nightly depth (300 seeds).
for (const row of statuses)
  test(`${row.name}: the run screen settles within a bounded number of calls`, async () => {
    for (const seed of SEEDS) {
      const seen = await observe(row, seed);
      const context = `seed ${seed}`;
      expect(seen.quiet, `${context}: still busy after ${QUIET_EVENTS} events`).toBe(true);
      expect(seen.calls, context).toBeLessThanOrEqual(BOUNDED_CALLS);
      expect(seen.screen.kind, context).toBe(row.code === 0 ? 'ready' : 'failed');
    }
  });

for (const row of failing) {
  test(`${row.name}: a final refusal is shown as a refusal, a transient one as unreachable`, async () => {
    for (const seed of SEEDS) {
      const {screen} = await observe(row, seed);
      if (screen.kind !== 'failed') throw new Error(`seed ${seed}: the screen is ${screen.kind}`);
      if (FINAL_CODES.has(row.code)) expect(screen.word, `seed ${seed}`).not.toBe('Unreachable');
      else expect(screen.word, `seed ${seed}`).toBe('Unreachable');
      if (row.code === 5) expect(screen.word, `seed ${seed}`).toBe('No run log');
    }
  });

  test(`${row.name}: the unary refusal keeps its gRPC status across the preload`, async () => {
    for (const seed of SEEDS)
      expect((await observe(row, seed)).unary?.code, `seed ${seed}`).toBe(row.code);
  });

  test(`${row.name}: the unary refusal carries the decoded diagnostic`, async () => {
    for (const seed of SEEDS) {
      const refusal = (await observe(row, seed)).unary;
      // The main process names the error class before the gateway's diagnostic.
      expect(refusal?.message.endsWith(row.message), `seed ${seed}: ${refusal?.message}`).toBe(
        true,
      );
    }
  });
}
