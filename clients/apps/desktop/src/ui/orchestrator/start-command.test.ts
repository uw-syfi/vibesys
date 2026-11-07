import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {Harness, ModelCatalogSchema, Refusal} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import {FakeDaemon, generateScenario, hostHarness} from '../../testing/orchestrator/index.js';
import {startIntent} from './intents.js';
import type {StartRunRequest} from './start-draft.js';

const request = (model: string): StartRunRequest => ({
  repository: '/work/decode-service',
  task: 'decode',
  fanoutLimit: 3,
  lead: {harness: Harness.CODEX, model},
  brief: 'No further steering.',
  maxTimeSeconds: 7200,
  maxCostUsd: 5,
});

const freshDaemon = () => new FakeDaemon(generateScenario({seed: 3, subagents: 1, live: true}));

describe('the Start command', () => {
  it.effect('sends the lead’s model and both bounds in the one command the host takes', () =>
    Effect.gen(function* () {
      const daemon = freshDaemon();
      const h = yield* hostHarness(daemon, {});
      const id = yield* h.session.commands.submit(startIntent(request('model-a')));
      yield* h.elapse(5);
      expect(yield* h.read(h.session.commands.progress(id))).toMatchObject({_tag: 'Accepted'});
      expect(daemon.started).toHaveLength(1);
      expect(daemon.started[0]).toMatchObject({
        lead: {harness: Harness.CODEX, model: 'model-a'},
        maxTime: {seconds: 7200n},
        maxCostUsd: 5,
      });
    }),
  );

  it.effect('leaves a bound out when it is none, so the host sets no limit', () =>
    Effect.gen(function* () {
      const daemon = freshDaemon();
      const h = yield* hostHarness(daemon, {});
      yield* h.session.commands.submit(
        startIntent({...request(''), maxTimeSeconds: null, maxCostUsd: null}),
      );
      yield* h.elapse(5);
      expect(daemon.started[0]?.maxTime).toBeUndefined();
      expect(daemon.started[0]?.maxCostUsd).toBeUndefined();
    }),
  );

  it.effect(
    'settles as refused, naming the model and the allowed list, when the host forbids it',
    () =>
      Effect.gen(function* () {
        const daemon = freshDaemon();
        daemon.setModels(create(ModelCatalogSchema, {allowedModels: ['model-a']}));
        const h = yield* hostHarness(daemon, {});
        const id = yield* h.session.commands.submit(startIntent(request('model-b')));
        yield* h.elapse(5);
        const progress = yield* h.read(h.session.commands.progress(id));
        expect(progress?._tag).toBe('Refused');
        if (progress?._tag !== 'Refused') return;
        expect(progress.receipt).toMatchObject({
          refusal: Refusal.MODEL_NOT_ALLOWED,
          model: 'model-b',
          allowedModels: ['model-a'],
        });
        expect(daemon.started).toEqual([]);
      }),
  );
});
