import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as TestClock from 'effect/testing/TestClock';
import {repositoryVerdict} from '../state/index.js';
import {FakeFiles} from '../testing/orchestrator/fake-files.js';
import {hostHarness} from '../testing/orchestrator/harness.js';
import {FakeDaemon, generateScenario, methodPathOf} from '../testing/orchestrator/index.js';
import {REPOSITORY_CHECK_DEBOUNCE} from './repository-check.js';

const scenario = generateScenario({seed: 1, subagents: 1, live: false});
const NOT_A_TREE = {reason: 'repository /plain is not a git work tree: it has no .git'};
const refuses = (repository: string) => (repository === '/plain' ? NOT_A_TREE : undefined);

const asked = (h: Effect.Success<ReturnType<typeof hostHarness>>) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf('CheckRepository')).length;

/** Type `path`, let typing pause, and read the verdict the form would show. */
const typeAndPause = (h: Effect.Success<ReturnType<typeof hostHarness>>, path: string) =>
  Effect.gen(function* () {
    const fiber = yield* Effect.forkChild(h.session.repositoryCheck.ask(path));
    yield* h.elapse(1);
    yield* Fiber.join(fiber);
    return repositoryVerdict(path, yield* h.read(h.session.repositoryCheck.state));
  });

describe('the repository check of the Start form', () => {
  it.effect('carries the tasks of a repository that passes, and none for one that does not', () =>
    Effect.gen(function* () {
      const files = new FakeFiles('/home/dev', {
        folders: {
          repo: {repository: true, tasks: ['sampler', 'kv-cache']},
          bare: {repository: true},
          plain: {},
        },
      });
      const h = yield* hostHarness(new FakeDaemon(scenario), {files});
      expect(yield* typeAndPause(h, '/repo')).toEqual({
        _tag: 'Valid',
        tasks: ['kv-cache', 'sampler'],
      });
      expect(yield* typeAndPause(h, '/bare')).toEqual({_tag: 'Valid', tasks: []});
      expect(yield* typeAndPause(h, '/plain')).toMatchObject({_tag: 'Invalid'});
    }),
  );

  it.effect(
    'shows the host refusal for a path that is not a work tree, and a pass for one that is',
    () =>
      Effect.gen(function* () {
        const h = yield* hostHarness(new FakeDaemon(scenario), {checkRepository: refuses});
        expect(yield* typeAndPause(h, '/plain')).toEqual({
          _tag: 'Invalid',
          reason: NOT_A_TREE.reason,
        });
        expect(yield* typeAndPause(h, '/repo')).toEqual({_tag: 'Valid', tasks: []});
      }),
  );

  it.effect('asks once for a path typed in pieces, and never about a prefix', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario), {checkRepository: refuses});
      const typing = [];
      for (const prefix of ['/p', '/pl', '/pla', '/plai', '/plain'])
        typing.push(yield* Effect.forkChild(h.session.repositoryCheck.ask(prefix)));
      yield* h.elapse(1);
      for (const fiber of typing) yield* Fiber.join(fiber);
      expect(asked(h)).toBe(1);
      const calls = h.fake
        .calls()
        .filter(call => call.target.method === methodPathOf('CheckRepository'));
      expect(calls).toHaveLength(1);
    }),
  );

  it.effect('does not ask before typing has paused', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario), {checkRepository: refuses});
      yield* Effect.forkChild(h.session.repositoryCheck.ask('/plain'));
      yield* h.elapse(0);
      expect(asked(h)).toBe(0);
      yield* TestClock.adjust(REPOSITORY_CHECK_DEBOUNCE);
      yield* h.elapse(0);
      expect(asked(h)).toBe(1);
    }),
  );

  it.effect(
    'never shows the answer about an earlier path as the verdict for the one now typed',
    () =>
      Effect.gen(function* () {
        const h = yield* hostHarness(new FakeDaemon(scenario), {checkRepository: refuses});
        yield* typeAndPause(h, '/plain');
        const checked = yield* h.read(h.session.repositoryCheck.state);
        expect(repositoryVerdict('/plain-2', checked)).toEqual({_tag: 'Checking'});
        expect(repositoryVerdict('', checked)).toEqual({_tag: 'Empty'});
      }),
  );

  it.effect(
    'leaves a path unblocked when the host cannot say: an older host, or one that cannot launch',
    () =>
      Effect.gen(function* () {
        const older = yield* hostHarness(new FakeDaemon(scenario), {checkRepository: 'unserved'});
        expect(yield* typeAndPause(older, '/plain')).toEqual({_tag: 'Valid', tasks: []});
        const unready = yield* hostHarness(new FakeDaemon(scenario), {
          checkRepository: () => ({reason: 'no agent can be launched', notReady: true}),
        });
        expect(yield* typeAndPause(unready, '/plain')).toEqual({_tag: 'Valid', tasks: []});
      }),
  );
});
