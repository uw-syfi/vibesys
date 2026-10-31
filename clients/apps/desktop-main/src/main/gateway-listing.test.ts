import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {create, toBinary} from '@bufbuild/protobuf';
import {hostListingFromRuns, RUN_WATCH_LEASE_MS, runWatch} from '@vibesys/connection';
import {FakeRunGateway} from '@vibesys/connection/testing';
import {
  dashboardRows,
  liveRows,
  machineLoads,
  runProjectRoot,
  runsKnown,
  SIDEBAR_EMPTY,
  sidebarStep,
} from '@vibesys/feature-app-shell/state';
import {
  type HostId,
  hostKey,
  LISTING_STEPS,
  type MachineLoad,
  recordedProjectRoot,
} from '@vibesys/hosts';
import {
  acceptListRunDigests,
  RunHeadSchema,
  RunLiveness,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {knownProjects} from '@vibesys/runs';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};

// Generate canonical roots, hosts' live/ended observations and replay counts. Recent is always
// empty: the gateway's address alone must carry a previously unvisited run into every consumer.
test('property: an unvisited run keeps its project through the wire and all listing consumers', () => {
  for (let seed = 0; seed < 32; seed += 1) {
    const project = `/projects/${seed % 2 ? 'space in name' : 'project'}/${seed}`;
    const runId = `run-${seed}`;
    const liveness = seed % 3 === 0 ? RunLiveness.ENDED : RunLiveness.ALIVE;
    const gateway = new FakeRunGateway();
    const watch = runWatch(gateway, new FakeClock().schedule);
    let state = SIDEBAR_EMPTY;
    const requests: ReturnType<typeof sidebarStep>['requests'][number][] = [];
    watch.observe(HOST, observed => {
      const listing = hostListingFromRuns(hostKey(HOST), observed);
      const step = sidebarStep(state, {
        type: 'overview',
        listings: [listing],
        recent: [],
        skippedRecent: [],
      });
      state = step.state;
      requests.push(...step.requests);
      if (listing.kind === 'listed')
        expect(knownProjects(listing, [], hostKey(HOST))).toEqual([project]);
    });
    const run = {
      projectRoot: project,
      runId,
      liveness,
      head: create(RunHeadSchema, {
        strategy: 'search',
        epoch: 1,
        committedSeq: 2n,
        lifecycle: {
          state:
            liveness === RunLiveness.ALIVE
              ? {case: 'running', value: {}}
              : {case: 'ended', value: {outcome: 1}},
        },
      }),
    };
    gateway.announce(HOST, [run]);
    for (let replay = 0; replay < seed % 4; replay += 1) gateway.update(HOST, run);
    expect(dashboardRows(state, {now: 1700000000000})).toHaveLength(1);
    expect(runProjectRoot(state, {host: hostKey(HOST), runId})).toBe(recordedProjectRoot(project));
    expect(
      requests.some(request => request.kind === 'list-project-runs' && request.project === project),
    ).toBe(true);
    const digests = requests.filter(request => request.kind === 'read-digests');
    expect(digests.length).toBeGreaterThan(0);
    for (const request of digests) {
      expect(request.project).toBe(project);
      expect(() =>
        acceptListRunDigests({
          requestId: 'request-a',
          project: request.project,
          runIds: [...request.runIds],
          withDashboard: true,
        }),
      ).not.toThrow();
    }
    if (liveness === RunLiveness.ALIVE) expect(liveRows(state)[0]?.title).toBe(String(seed));
    watch.close();
  }
});

test('missing or invalid projects stay listed as typed unknown projects', () => {
  for (const projectRoot of [undefined, '', 'relative']) {
    const gateway = new FakeRunGateway();
    const watch = runWatch(gateway, new FakeClock().schedule);
    let delivered = false;
    watch.observe(HOST, observed => {
      if (observed.phase.kind !== 'live') return;
      delivered = true;
      const listing = hostListingFromRuns(hostKey(HOST), observed);
      const {state} = sidebarStep(SIDEBAR_EMPTY, {
        type: 'overview',
        listings: [listing],
        recent: [],
        skippedRecent: [],
      });
      const rows = dashboardRows(state, {now: 1700000000000});
      expect(rows).toHaveLength(1);
      expect(runsKnown(state, [hostKey(HOST)])).toBe('some');
      expect(rows[0]).toMatchObject({
        project: null,
        row: {title: 'Unknown project', status: 'live', detail: 'host-a · starting'},
      });
      expect(runProjectRoot(state, {host: hostKey(HOST), runId: 'run-a'})).toBeUndefined();
      const remembered = {
        ...state,
        recent: [
          {
            host: hostKey(HOST),
            hostLabel: 'host-a',
            project: '/projects/stale',
            task: null,
            runId: 'run-a',
            instanceId: null,
            attachedAt: 0,
          },
        ],
      };
      expect(runProjectRoot(remembered, {host: hostKey(HOST), runId: 'run-a'})).toBeUndefined();
      expect(listing).toMatchObject({
        live: [],
        ended: [],
        unverified: [],
        unknownProject: [{runId: 'run-a', phase: 'starting', alive: true}],
      });
    });
    const stream = gateway.open(HOST)[0];
    if (stream === undefined) throw new Error('missing gateway stream');
    stream.receive(
      toBinary(
        WatchRunsResponseSchema,
        create(WatchRunsResponseSchema, {
          runId: 'run-a',
          projectRoot,
          liveness: RunLiveness.ALIVE,
        }),
      ),
    );
    stream.receive(
      toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, {initialComplete: true})),
    );
    expect(delivered).toBe(true);
    watch.close();
  }
});

// The response is pinned by the Go test over this previous writer's addressless index/head.
test('retained addressless publisher reaches dashboard, Usage projects and control address', () => {
  const corpus = new URL('../../../../../proto/testdata/retained/', import.meta.url);
  const manifest = JSON.parse(readFileSync(new URL('manifest.json', corpus), 'utf8')) as Record<
    string,
    {project_root?: string}
  >;
  for (const [name, facts] of Object.entries(manifest)) {
    const project = facts.project_root;
    if (project === undefined) continue;
    const gateway = new FakeRunGateway();
    const watch = runWatch(gateway, new FakeClock().schedule);
    let state = SIDEBAR_EMPTY;
    watch.observe(HOST, observed => {
      const listing = hostListingFromRuns(hostKey(HOST), observed);
      state = sidebarStep(state, {
        type: 'overview',
        listings: [listing],
        recent: [],
        skippedRecent: [],
      }).state;
      if (listing.kind === 'listed')
        expect(knownProjects(listing, [], hostKey(HOST))).toEqual([project]);
    });
    const stream = gateway.open(HOST)[0];
    if (stream === undefined) throw new Error('missing gateway stream');
    stream.receive(readFileSync(fileURLToPath(new URL(`${name}/watch.response.pb`, corpus))));
    stream.receive(
      toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, {initialComplete: true})),
    );
    expect(dashboardRows(state, {now: 1700000000000})[0]?.project).toBe(project);
    expect(runProjectRoot(state, {host: hostKey(HOST), runId: 'run-1'})).toBe(
      recordedProjectRoot(project),
    );
    watch.close();
  }
});

const MACHINES: readonly HostId[] = [{kind: 'local'}, HOST, {kind: 'ssh', alias: 'host-b'}];

type HostEvent =
  | {readonly kind: 'step'; readonly step: (typeof LISTING_STEPS)[number]}
  | {readonly kind: 'exit'}
  | {readonly kind: 'error'}
  | {readonly kind: 'list'}
  | {readonly kind: 'silence'; readonly ms: number};

const HOST_EVENT: fc.Arbitrary<HostEvent> = fc.oneof(
  fc.constantFrom(...LISTING_STEPS).map(step => ({kind: 'step', step}) as const),
  fc.constant({kind: 'exit'} as const),
  fc.constant({kind: 'error'} as const),
  fc.constant({kind: 'list'} as const),
  fc.integer({min: 1, max: 2 * RUN_WATCH_LEASE_MS}).map(ms => ({kind: 'silence', ms}) as const),
);

/**
 * The longest a host that never answers can leave its machine unnamed as stopped: one lease per try
 * until the views call it offline, with the doubling delays between them.
 */
const NEVER_ANSWERS_BOUND_STEPS: readonly number[] = [
  ...[1_000, 2_000, 4_000, 8_000, 16_000].flatMap(delay => [RUN_WATCH_LEASE_MS, delay]),
  RUN_WATCH_LEASE_MS,
];

/** Machines watched as the app watches them, folded as the Runs screen reads them. */
function runsScreenWorld() {
  const gateway = new FakeRunGateway();
  const clock = new FakeClock();
  const watch = runWatch(gateway, clock.schedule);
  let state = SIDEBAR_EMPTY;
  const hosts = MACHINES.map(hostKey);
  for (const host of MACHINES) {
    watch.observe(host, observed => {
      state = sidebarStep(state, {
        type: 'listing',
        listing: hostListingFromRuns(hostKey(host), observed),
      }).state;
    });
  }
  state = sidebarStep(state, {
    type: 'overview',
    listings: state.listings,
    recent: [],
    skippedRecent: [],
  }).state;
  return {
    gateway,
    clock,
    watch,
    loads: (): MachineLoad[] => machineLoads(state, hosts),
  };
}

/** Every stream's promise settling reaches the watch only after the microtasks it queued. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
}

function expectNamed(load: MachineLoad): void {
  if (load.kind === 'listed') return;
  expect(LISTING_STEPS).toContain(load.step);
  if (load.kind === 'failed') expect(load.reason).not.toBe('');
}

function happen(world: ReturnType<typeof runsScreenWorld>, host: HostId, event: HostEvent): void {
  const stream = world.gateway.open(host).at(-1);
  if (event.kind === 'step') stream?.reached(event.step);
  else if (event.kind === 'exit') stream?.end();
  else if (event.kind === 'error') stream?.fail(new Error('gateway startup exited: 1'));
  else if (event.kind === 'list') world.gateway.announce(host, []);
  else world.clock.advanceBy(event.ms);
}

test('property: at every moment of any host event sequence each machine is at a named step', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.array(fc.tuple(fc.constantFrom(...MACHINES), HOST_EVENT), {maxLength: 40}),
      async events => {
        const world = runsScreenWorld();
        for (const [host, event] of events) {
          happen(world, host, event);
          await settle();
          for (const load of world.loads()) expectNamed(load);
        }
        // From wherever the sequence left it, a host that says nothing more stops being waited on.
        for (const each of NEVER_ANSWERS_BOUND_STEPS) {
          world.clock.advanceBy(each);
          await settle();
        }
        expect(world.loads().map(load => load.kind)).not.toContain('loading');
        world.watch.close();
      },
    ),
    {numRuns: 64},
  );
});

test('a host that never answers ends failed at the step it reached, with a reason', async () => {
  const world = runsScreenWorld();
  expect(world.loads().map(load => load.kind)).toEqual(['loading', 'loading', 'loading']);
  for (const stream of world.gateway.open(HOST)) stream.reached('handshake');
  for (const each of NEVER_ANSWERS_BOUND_STEPS) {
    for (const stream of world.gateway.open(HOST)) stream.reached('handshake');
    world.clock.advanceBy(each);
    await settle();
  }
  expect(world.loads()[1]).toEqual({
    kind: 'failed',
    host: hostKey(HOST),
    step: 'handshake',
    reason: 'no answer',
  });
  world.watch.close();
});

test('a helper that exits at once fails its machine, and retry starts the steps over', async () => {
  const world = runsScreenWorld();
  for (let attempt = 0; attempt < 5; attempt += 1) {
    for (const stream of world.gateway.open(HOST)) {
      stream.fail(new Error('Error: gateway startup exited: 1'));
    }
    await settle();
    world.clock.advanceBy(RUN_WATCH_LEASE_MS);
    await settle();
  }
  expect(world.loads()[1]).toMatchObject({
    kind: 'failed',
    step: 'gateway',
    reason: 'gateway exited (code 1)',
  });
  world.watch.retry(HOST);
  expect(world.loads()[1]).toEqual({
    kind: 'loading',
    host: hostKey(HOST),
    step: 'gateway',
    attempt: 0,
  });
  world.gateway.announce(HOST, []);
  expect(world.loads()[1]).toEqual({kind: 'listed', host: hostKey(HOST)});
  world.watch.close();
});
