import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentSpecSchema,
  CatalogueProblemReason,
  FitState,
  Harness,
  StartRunSchema,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import {NOT_READ} from '../state/index.js';
import {type HostHarness, hostHarness} from '../testing/orchestrator/harness.js';
import {FakeDaemon, generateScenario, methodPathOf} from '../testing/orchestrator/index.js';
import {
  fixtureCatalogueProblemResponse,
  FIXTURE_REPOSITORIES_DIRECTORY,
} from '../testing/orchestrator/new-run-fixtures.js';
import type {Intent} from './commands.js';

const scenario = generateScenario({seed: 1, subagents: 1, live: false});

const startIn =
  (task: string) =>
  (repository: string): Intent => ({
    case: 'startRun',
    value: create(StartRunSchema, {
      repository,
      task,
      fanoutLimit: 3,
      lead: create(AgentSpecSchema, {harness: Harness.CLAUDE_CODE}),
      brief: 'No further steering.',
    }),
  });

const callsTo = (h: HostHarness, method: string) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf(method));

describe('what the New run screen asks of the host', () => {
  it.effect('holds nothing before the screen asks, then the catalogue and places as sent', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario));
      expect(yield* h.read(h.session.newRun.catalogue)).toEqual(NOT_READ);
      yield* h.session.newRun.load;
      const catalogue = yield* h.read(h.session.newRun.catalogue);
      expect(catalogue).toMatchObject({reading: false, failure: null});
      expect(catalogue.data?.repositoriesDirectory).toBe(FIXTURE_REPOSITORIES_DIRECTORY);
      expect(catalogue.data?.entries.map(entry => entry.fit?.state)).toContain(FitState.FITS);
      const places = yield* h.read(h.session.newRun.places);
      expect(places.data?.places.map(place => place.name)).toEqual([
        'local',
        'host-a',
        'cluster-a',
        'cluster-b',
      ]);
    }),
  );

  it.effect('reads a classified catalogue problem as a successful reply, not a failure', () =>
    Effect.gen(function* () {
      const problem = fixtureCatalogueProblemResponse(
        CatalogueProblemReason.NO_RELEASE,
        'no release found next to this host',
      );
      const h = yield* hostHarness(new FakeDaemon(scenario), {catalogue: problem});
      yield* h.session.newRun.load;
      const catalogue = yield* h.read(h.session.newRun.catalogue);
      expect(catalogue).toMatchObject({reading: false, failure: null});
      expect(catalogue.data?.entries).toEqual([]);
      expect(catalogue.data?.repositoriesDirectory).toBe(FIXTURE_REPOSITORIES_DIRECTORY);
      expect(catalogue.data?.problem).toEqual(problem.problem);
    }),
  );

  it.effect('reads only the places for the Machines page, and again on a refresh', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario));
      yield* h.session.newRun.loadPlaces;
      expect(callsTo(h, 'ListEvalPlaces')).toHaveLength(1);
      expect(callsTo(h, 'ListCatalogue')).toHaveLength(0);
      yield* h.session.newRun.refreshPlaces;
      expect(callsTo(h, 'ListEvalPlaces')).toHaveLength(2);
      const places = yield* h.read(h.session.newRun.places);
      expect(places).toMatchObject({reading: false, failure: null});
      expect(places.data?.places).toHaveLength(4);
    }),
  );

  it.effect('makes the repository, then starts the run in it, as one step', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario));
      const id = yield* h.session.newRun.startFromExample(
        {example: 'data-structures/swmr-map', name: 'swmr-map', parent: '/work'},
        startIn('swmr-map'),
      );
      expect(id).toBeDefined();
      expect(yield* h.read(h.session.newRun.provision)).toEqual({
        _tag: 'Done',
        repository: '/work/swmr-map',
      });
      expect(callsTo(h, 'Dispatch')).toHaveLength(1);
    }),
  );

  it.effect('starts no run when the host refuses the repository, and says why', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario));
      const first = yield* h.session.newRun.startFromExample(
        {example: 'databases/kv-store', name: 'kv-store', parent: ''},
        startIn('kv-store'),
      );
      const second = yield* h.session.newRun.startFromExample(
        {example: 'databases/kv-store', name: 'kv-store', parent: ''},
        startIn('kv-store'),
      );
      expect(first).toBeDefined();
      expect(second).toBeUndefined();
      expect(yield* h.read(h.session.newRun.provision)).toMatchObject({
        _tag: 'Refused',
        reason: 'NAME_TAKEN',
      });
      const needs = yield* h.session.newRun.startFromExample(
        {example: 'model-serving/ember', name: 'ember', parent: ''},
        startIn('ember'),
      );
      expect(needs).toBeUndefined();
      expect(yield* h.read(h.session.newRun.provision)).toMatchObject({
        _tag: 'Refused',
        reason: 'NEEDS_NETWORK',
      });
      expect(callsTo(h, 'Dispatch')).toHaveLength(1);
    }),
  );

  it.effect('forgets the last outcome when another example is picked', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario));
      yield* h.session.newRun.startFromExample(
        {example: 'nothing/here', name: 'here', parent: ''},
        startIn('here'),
      );
      expect(yield* h.read(h.session.newRun.provision)).toMatchObject({_tag: 'Refused'});
      yield* h.session.newRun.forgetProvision;
      expect(yield* h.read(h.session.newRun.provision)).toEqual({_tag: 'Idle'});
    }),
  );
});
