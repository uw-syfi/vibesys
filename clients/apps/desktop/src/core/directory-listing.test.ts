import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import {listingKey} from '../state/index.js';
import {FakeFiles, MAX_ENTRIES} from '../testing/orchestrator/fake-files.js';
import {hostHarness} from '../testing/orchestrator/harness.js';
import {FakeDaemon, generateScenario, methodPathOf} from '../testing/orchestrator/index.js';

const scenario = generateScenario({seed: 1, subagents: 1, live: false});

const FILES = new FakeFiles('/home/dev', {
  folders: {
    home: {
      folders: {
        dev: {
          folders: {
            code: {folders: {app: {repository: true, tasks: ['b', 'a']}, '.cache': {}}},
            wide: {
              files: Array.from(
                {length: MAX_ENTRIES + 20},
                (_, i) => `f${String(i).padStart(4, '0')}`,
              ),
            },
          },
          files: ['notes.md'],
        },
      },
    },
  },
});

const asked = (h: Effect.Success<ReturnType<typeof hostHarness>>) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf('ListDirectory')).length;

const listingOf = (
  h: Effect.Success<ReturnType<typeof hostHarness>>,
  folder: string,
  hidden = false,
) =>
  Effect.gen(function* () {
    const all = yield* h.read(h.session.directories.state);
    return all.get(listingKey(folder, hidden));
  });

describe('the host’s folder listings kept for the Start form', () => {
  it.effect(
    'asks for a folder once however often it is wanted, and lists what the host listed',
    () =>
      Effect.gen(function* () {
        const h = yield* hostHarness(new FakeDaemon(scenario), {files: FILES});
        for (let i = 0; i < 5; i++) yield* h.session.directories.want('/home/dev/', false, false);
        expect(asked(h)).toBe(1);
        expect(yield* listingOf(h, '/home/dev/')).toMatchObject({
          _tag: 'Listed',
          truncated: false,
          entries: [
            {name: 'code', isDirectory: true},
            {name: 'notes.md', isDirectory: false},
            {name: 'wide', isDirectory: true},
          ],
        });
      }),
  );

  it.effect('lists the hidden entries of a folder apart from its visible ones', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario), {files: FILES});
      yield* h.session.directories.want('~/code/', false, false);
      yield* h.session.directories.want('~/code/', true, false);
      expect(asked(h)).toBe(2);
      const visible = yield* listingOf(h, '~/code/', false);
      const hidden = yield* listingOf(h, '~/code/', true);
      expect(visible?._tag === 'Listed' && visible.entries.map(e => e.name)).toEqual(['app']);
      expect(hidden?._tag === 'Listed' && hidden.entries.map(e => e.name)).toEqual([
        '.cache',
        'app',
      ]);
    }),
  );

  it.effect('carries the host’s marks and its cap', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario), {files: FILES});
      yield* h.session.directories.want('/home/dev/code/', false, false);
      yield* h.session.directories.want('/home/dev/wide/', false, false);
      expect(yield* listingOf(h, '/home/dev/code/')).toMatchObject({
        entries: [{name: 'app', isGitRepository: true, hasTasks: true}],
      });
      const wide = yield* listingOf(h, '/home/dev/wide/');
      expect(wide?._tag === 'Listed' && [wide.entries.length, wide.truncated]).toEqual([
        MAX_ENTRIES,
        true,
      ]);
    }),
  );

  it.effect(
    'keeps the host’s refusal as the answer for the folder, naming it, and asks no more',
    () =>
      Effect.gen(function* () {
        const h = yield* hostHarness(new FakeDaemon(scenario), {files: FILES});
        for (const folder of ['/missing/', '/home/dev/notes.md/']) {
          yield* h.session.directories.want(folder, false, false);
          yield* h.session.directories.want(folder, false, false);
        }
        const gone = yield* listingOf(h, '/missing/');
        expect(gone?._tag === 'Failed' && gone.reason).toContain('/missing');
        expect(yield* listingOf(h, '/home/dev/notes.md/')).toMatchObject({_tag: 'Failed'});
        expect(asked(h)).toBe(2);
      }),
  );

  it.effect('is a refusal for every folder on a host that does not serve the call', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario));
      yield* h.session.directories.want('/home/', false, false);
      expect(yield* listingOf(h, '/home/')).toMatchObject({_tag: 'Failed'});
    }),
  );

  it.effect('reads a folder again when it is wanted afresh, and drops the others', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario), {files: FILES});
      yield* h.session.directories.want('/home/dev/', false, false);
      yield* h.session.directories.want('/home/dev/code/', false, false);
      yield* h.session.directories.want('/home/dev/', false, true);
      expect(asked(h)).toBe(3);
      expect(yield* listingOf(h, '/home/dev/')).toMatchObject({_tag: 'Listed'});
      expect(yield* listingOf(h, '/home/dev/code/')).toBeUndefined();
    }),
  );
});
