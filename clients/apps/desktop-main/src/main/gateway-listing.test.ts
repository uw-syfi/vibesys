import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {create, toBinary} from '@bufbuild/protobuf';
import {hostListingFromRuns, runWatch} from '@vibesys/connection';
import {FakeRunGateway} from '@vibesys/connection/testing';
import {
  dashboardRows,
  liveRows,
  runProjectRoot,
  runsKnown,
  SIDEBAR_EMPTY,
  sidebarStep,
} from '@vibesys/feature-app-shell/state';
import {type HostId, hostKey, recordedProjectRoot} from '@vibesys/hosts';
import {
  acceptListRunDigests,
  RunHeadSchema,
  RunLiveness,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {knownProjects} from '@vibesys/runs';
import {FakeClock} from '@vibesys/transport/testing';

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
        row: {title: 'Unknown project', detail: 'Project unknown'},
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
        unknownProject: [{runId: 'run-a', phase: 'starting'}],
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
