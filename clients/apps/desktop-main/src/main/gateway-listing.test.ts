import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {create, fromBinary} from '@bufbuild/protobuf';
import {GATEWAY_FIRST_CONNECT_BUDGET_MS, hostRuns} from '@vibesys/connection';
import {
  announce,
  announceChange,
  announceEntries,
  hubRig,
  settle,
} from '@vibesys/connection/testing';
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
  RunEntrySchema,
  RunHeadSchema,
  RunLiveness,
  TopicEntrySchema,
} from '@vibesys/proto';
import {knownProjects} from '@vibesys/runs';
import {
  backoffMs,
  FAILURE_WORDS,
  failureBoundMs,
  OFFLINE_AFTER,
  runs as runsTopic,
} from '@vibesys/subscription';
import fc from 'fast-check';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};

// Generate canonical roots, hosts' live/ended observations and replay counts. Recent is always
// empty: the gateway's address alone must carry a previously unvisited run into every consumer.
test('property: an unvisited run keeps its project through the wire and all listing consumers', () => {
  for (let seed = 0; seed < 32; seed += 1) {
    const project = `/projects/${seed % 2 ? 'space in name' : 'project'}/${seed}`;
    const runId = `run-${seed}`;
    const liveness = seed % 3 === 0 ? RunLiveness.ENDED : RunLiveness.ALIVE;
    const {gateway, subscriptions} = hubRig();
    let state = SIDEBAR_EMPTY;
    const requests: ReturnType<typeof sidebarStep>['requests'][number][] = [];
    const release = hostRuns(subscriptions).observe(hostKey(HOST), listing => {
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
    announce(gateway, hostKey(HOST), [run]);
    for (let replay = 0; replay < seed % 4; replay += 1)
      announceChange(gateway, hostKey(HOST), run);
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
    release();
  }
});

test('a run without a project stays listed as a typed unknown project', () => {
  const {gateway, subscriptions} = hubRig();
  let delivered = false;
  const release = hostRuns(subscriptions).observe(hostKey(HOST), listing => {
    if (listing.kind !== 'listed') return;
    delivered = true;
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
  announceEntries(gateway, hostKey(HOST), [
    create(TopicEntrySchema, {
      key: 'run-a',
      value: {case: 'runs', value: {liveness: RunLiveness.ALIVE}},
    }),
  ]);
  expect(delivered).toBe(true);
  release();
});

// An address the schema refuses never reaches a listing: the stream carrying it fails as invalid.
test('an empty or relative project address is refused, and the host is not listed', () => {
  for (const projectRoot of ['', 'relative']) {
    const {gateway, subscriptions} = hubRig();
    const listings: string[] = [];
    const release = hostRuns(subscriptions).observe(hostKey(HOST), listing =>
      listings.push(listing.kind),
    );
    announceEntries(gateway, hostKey(HOST), [
      create(TopicEntrySchema, {
        key: 'run-a',
        value: {case: 'runs', value: {projectRoot, liveness: RunLiveness.ALIVE}},
      }),
    ]);
    expect(listings).not.toContain('listed');
    expect(gateway.open).toHaveLength(0);
    release();
  }
});

// The entry is pinned by the Go test over this previous writer's addressless index/head.
test('retained addressless publisher reaches dashboard, Usage projects and control address', () => {
  const corpus = new URL('../../../../../proto/testdata/retained/', import.meta.url);
  const manifest = JSON.parse(readFileSync(new URL('manifest.json', corpus), 'utf8')) as Record<
    string,
    {project_root?: string}
  >;
  for (const [name, facts] of Object.entries(manifest)) {
    const project = facts.project_root;
    if (project === undefined) continue;
    const {gateway, subscriptions} = hubRig();
    let state = SIDEBAR_EMPTY;
    const release = hostRuns(subscriptions).observe(hostKey(HOST), listing => {
      state = sidebarStep(state, {
        type: 'overview',
        listings: [listing],
        recent: [],
        skippedRecent: [],
      }).state;
      if (listing.kind === 'listed')
        expect(knownProjects(listing, [], hostKey(HOST))).toEqual([project]);
    });
    const entry = fromBinary(
      RunEntrySchema,
      readFileSync(fileURLToPath(new URL(`${name}/run.entry.pb`, corpus))),
    );
    announceEntries(gateway, hostKey(HOST), [
      create(TopicEntrySchema, {key: 'run-1', value: {case: 'runs', value: entry}}),
    ]);
    expect(dashboardRows(state, {now: 1700000000000})[0]?.project).toBe(project);
    expect(runProjectRoot(state, {host: hostKey(HOST), runId: 'run-1'})).toBe(
      recordedProjectRoot(project),
    );
    release();
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
  fc.integer({min: 1, max: 120_000}).map(ms => ({kind: 'silence', ms}) as const),
);

/** The longest a host that sends nothing at all can leave its machine unnamed as stopped. */
const NEVER_ANSWERS_BOUND_MS = failureBoundMs(runsTopic, GATEWAY_FIRST_CONNECT_BUDGET_MS);

/** Machines watched as the app watches them, folded as the Runs screen reads them. */
function runsScreenWorld() {
  const rig = hubRig();
  const runs = hostRuns(rig.subscriptions);
  let state = SIDEBAR_EMPTY;
  const hosts = MACHINES.map(hostKey);
  const releases = MACHINES.map(host =>
    runs.observe(hostKey(host), listing => {
      state = sidebarStep(state, {type: 'listing', listing}).state;
    }),
  );
  state = sidebarStep(state, {
    type: 'overview',
    listings: state.listings,
    recent: [],
    skippedRecent: [],
  }).state;
  return {
    ...rig,
    runs,
    release: () => {
      for (const release of releases) release();
    },
    loads: (): MachineLoad[] => machineLoads(state, hosts),
  };
}

function expectNamed(load: MachineLoad): void {
  if (load.kind === 'listed') return;
  expect(LISTING_STEPS).toContain(load.step);
  if (load.kind === 'failed') expect(load.reason).not.toBe('');
}

function happen(world: ReturnType<typeof runsScreenWorld>, host: HostId, event: HostEvent): void {
  const stream = world.gateway.open.filter(each => each.host === hostKey(host)).at(-1);
  if (event.kind === 'step') stream?.reached(event.step);
  else if (event.kind === 'exit') stream?.end();
  else if (event.kind === 'error') stream?.fail(new Error('gateway startup exited: 1'));
  else if (event.kind === 'list') announce(world.gateway, hostKey(host), []);
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
        // From wherever the sequence left it, a host that says nothing more stops being waited on,
        // unless it has listed: what it said is then a fact until its stream is lost.
        for (let elapsed = 0; elapsed < NEVER_ANSWERS_BOUND_MS; elapsed += 30_000) {
          world.clock.advanceBy(30_000);
          await settle();
        }
        expect(world.loads().map(load => load.kind)).not.toContain('loading');
        world.release();
      },
    ),
    {numRuns: 64},
  );
});

test('a host that never answers ends failed at the step it reached, with the word of its reason', async () => {
  const world = runsScreenWorld();
  expect(world.loads().map(load => load.kind)).toEqual(['loading', 'loading', 'loading']);
  for (let elapsed = 0; elapsed < NEVER_ANSWERS_BOUND_MS; elapsed += 30_000) {
    for (const stream of world.gateway.open.filter(each => each.host === hostKey(HOST)))
      stream.reached('handshake');
    world.clock.advanceBy(30_000);
    await settle();
  }
  expect(world.loads()[1]).toEqual({
    kind: 'failed',
    host: hostKey(HOST),
    step: 'handshake',
    reason: FAILURE_WORDS.unreachable,
  });
  world.release();
});

test('a helper that exits at once fails its machine, and retry starts the steps over', async () => {
  const world = runsScreenWorld();
  for (let attempt = 1; attempt <= OFFLINE_AFTER; attempt += 1) {
    for (const stream of world.gateway.open.filter(each => each.host === hostKey(HOST)))
      stream.fail(new Error('Error: gateway startup exited: 1'));
    await settle();
    world.clock.advanceBy(backoffMs(attempt));
    await settle();
  }
  expect(world.loads()[1]).toMatchObject({
    kind: 'failed',
    step: 'gateway',
    reason: FAILURE_WORDS.unreachable,
  });
  world.runs.retry(hostKey(HOST));
  expect(world.loads()[1]).toEqual({
    kind: 'loading',
    host: hostKey(HOST),
    step: 'gateway',
    attempt: 0,
  });
  announce(world.gateway, hostKey(HOST), []);
  expect(world.loads()[1]).toEqual({kind: 'listed', host: hostKey(HOST)});
  world.release();
});
