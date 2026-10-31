import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  hostListingFromRuns,
  type RunsState,
  type WatchedRun,
  watchedListing,
} from '@vibesys/connection';
import {type HostKey, LISTING_STEPS, recordedProjectRoot} from '@vibesys/hosts';
import {
  DrainGoal,
  LifecycleSchema,
  Outcome,
  RunHeadSchema,
  RunLiveness,
  StageState,
  StartupStage,
} from '@vibesys/proto';
import fc from 'fast-check';

const HOST = 'ssh:host-a' as HostKey;

const LIFECYCLE = fc.constantFrom(
  create(LifecycleSchema, {
    state: {
      case: 'starting',
      value: {
        stage: StartupStage.BUILDING_IMAGE,
        state: StageState.RUNNING,
        progress: {done: 3n, total: 9n, label: 'RUN make'},
      },
    },
  }),
  create(LifecycleSchema, {state: {case: 'running', value: {}}}),
  create(LifecycleSchema, {
    state: {case: 'running', value: {hold: {reason: {case: 'operator', value: {}}}}},
  }),
  create(LifecycleSchema, {
    state: {case: 'running', value: {hold: {reason: {case: 'capacitySpan', value: 'span-1'}}}},
  }),
  create(LifecycleSchema, {state: {case: 'draining', value: {goal: DrainGoal.PARK}}}),
  create(LifecycleSchema, {state: {case: 'draining', value: {goal: DrainGoal.STOP}}}),
  create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
  ...[Outcome.FINISHED, Outcome.STOPPED, Outcome.INTERRUPTED].map(outcome =>
    create(LifecycleSchema, {state: {case: 'ended', value: {outcome}}}),
  ),
  create(LifecycleSchema, {
    state: {
      case: 'ended',
      value: {outcome: Outcome.FAILED, failure: {kind: 1, reason: 'build failed'}},
    },
  }),
);

const WATCHED = fc.record({
  head: fc.option(
    fc.record({
      lifecycle: fc.option(LIFECYCLE, {nil: undefined}),
      started: fc.option(fc.integer({min: 1, max: 4_000_000_000}), {nil: undefined}),
    }),
    {nil: undefined},
  ),
  liveness: fc.constantFrom(
    RunLiveness.UNSPECIFIED,
    RunLiveness.ALIVE,
    RunLiveness.INTERRUPTED,
    RunLiveness.ENDED,
    RunLiveness.PARKED,
  ),
});

function watched(input: {
  head:
    | {
        lifecycle: ReturnType<typeof create<typeof LifecycleSchema>> | undefined;
        started: number | undefined;
      }
    | undefined;
  liveness: RunLiveness;
}): WatchedRun {
  const head = input.head;
  return {
    projectRoot: recordedProjectRoot('/project'),
    liveness: input.liveness,
    head:
      head === undefined
        ? undefined
        : create(RunHeadSchema, {
            ...(head.lifecycle ? {lifecycle: head.lifecycle} : {awaitingLifecycle: true, epoch: 1}),
            ...(head.started === undefined
              ? {}
              : {process: {pid: 1, epoch: 1, startedAt: timestampFromMs(head.started * 1000)}}),
          }),
  };
}

const RUNS = fc
  .uniqueArray(fc.tuple(fc.stringMatching(/^[a-z][a-z0-9-]{0,11}$/), WATCHED), {
    selector: ([id]) => id,
    maxLength: 12,
  })
  .map(entries => new Map(entries.map(([id, run]) => [id, watched(run)])));

test('property: every run is listed once, by what its host says of its writer', () => {
  fc.assert(
    fc.property(RUNS, runs => {
      const listing = watchedListing(HOST, runs);
      if (listing.kind !== 'listed') throw new Error('listed required');
      const live = listing.live.map(run => run.runId);
      const ended = listing.ended.map(run => run.runId);
      expect([...live, ...ended, ...listing.unverified].sort()).toEqual([...runs.keys()].sort());
      for (const [id, run] of runs) {
        expect(live.includes(id)).toBe(run.liveness === RunLiveness.ALIVE);
        expect(listing.unverified.includes(id)).toBe(run.liveness === RunLiveness.UNSPECIFIED);
      }
      for (const run of [...listing.live, ...listing.ended]) {
        expect(run.status).not.toBe('');
        expect(run.paused !== null).toBe(run.phase === 'paused');
        expect(run.projectRoot).toBe('/project');
        expect(run.end).toBeNull();
      }
      for (const run of listing.ended)
        expect(['completed', 'failed', 'stopped']).toContain(run.phase);
    }),
    {numRuns: 100},
  );
});

test('property: the listing does not depend on the order the host announced its runs', () => {
  fc.assert(
    fc.property(RUNS, fc.nat(), (runs, turn) => {
      const entries = [...runs];
      const shifted = new Map([
        ...entries.slice(turn % (entries.length || 1)),
        ...entries.slice(0, turn % (entries.length || 1)),
      ]);
      expect(watchedListing(HOST, shifted)).toEqual(watchedListing(HOST, runs));
    }),
    {numRuns: 50},
  );
});

test('a writer that is dead without an ending reads as failed with a reason, a parked one as stopped', () => {
  const run = (
    liveness: RunLiveness,
    lifecycle: Parameters<typeof create<typeof LifecycleSchema>>[1],
  ) =>
    watched({head: {lifecycle: create(LifecycleSchema, lifecycle), started: undefined}, liveness});
  const listing = watchedListing(
    HOST,
    new Map([
      ['dead', run(RunLiveness.INTERRUPTED, {state: {case: 'running', value: {}}})],
      ['parked', run(RunLiveness.PARKED, {state: {case: 'parked', value: {}}})],
    ]),
  );
  if (listing.kind !== 'listed') throw new Error('listed required');
  expect(listing.ended.map(({runId, phase, reason}) => [runId, phase, reason !== null])).toEqual([
    ['dead', 'failed', true],
    ['parked', 'stopped', false],
  ]);
});

test('property: a host lists runs only once its stream has delivered them all, whatever else is up', () => {
  fc.assert(
    fc.property(
      RUNS,
      fc.oneof(
        fc.constantFrom(...LISTING_STEPS).map(step => ({kind: 'connecting' as const, step})),
        fc.constant({kind: 'live' as const}),
        fc.integer({min: 1, max: 12}).map(attempt => ({
          kind: 'reconnecting' as const,
          attempt,
          detail: 'network loss',
          step: 'runs' as const,
        })),
      ),
      (runs, phase) => {
        const state: RunsState = {phase, runs};
        const listing = hostListingFromRuns(HOST, state);
        expect(listing.kind).toBe(phase.kind === 'live' ? 'listed' : 'unavailable');
        if (listing.kind === 'unavailable' && phase.kind === 'reconnecting')
          expect(listing.connection.kind).toBe(phase.attempt >= 5 ? 'offline' : 'reconnecting');
        if (listing.kind === 'unavailable' && phase.kind === 'connecting')
          expect(listing).toMatchObject({connection: {kind: 'connecting'}, step: phase.step});
      },
    ),
    {numRuns: 60},
  );
});
