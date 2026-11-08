import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import {memoryFolders} from '../../testing/settings/machine-folders.js';
import type {MachineActions} from './actions.js';
import {makeMachineFolders} from './folders.js';

describe('the folders the window holds', () => {
  it.effect('reads what the main process keeps, and follows an accepted save', () =>
    Effect.gen(function* () {
      const memory = memoryFolders({'ssh:host-a': '~/work'});
      const folders = yield* makeMachineFolders(memory.actions);
      const registry = AtomRegistry.make();
      expect(registry.get(folders.state)).toEqual({});
      yield* folders.load;
      expect(registry.get(folders.state)).toEqual({'ssh:host-a': '~/work'});
      expect(yield* folders.save('local', '/data')).toMatchObject({kind: 'saved'});
      expect(registry.get(folders.state)).toEqual({'ssh:host-a': '~/work', local: '/data'});
      yield* folders.save('local', '');
      expect(registry.get(folders.state)).toEqual({'ssh:host-a': '~/work'});
    }),
  );

  it.effect('leaves the folders as they were when a save is refused', () =>
    Effect.gen(function* () {
      const memory = memoryFolders({local: '/data'});
      const refusing: Pick<MachineActions, 'repositories' | 'setRepositories'> = {
        ...memory.actions,
        setRepositories: () => Effect.succeed({kind: 'refused', reason: 'not saved'} as const),
      };
      const folders = yield* makeMachineFolders(refusing);
      const registry = AtomRegistry.make();
      yield* folders.load;
      expect(yield* folders.save('ssh:host-b', '/x')).toEqual({
        kind: 'refused',
        reason: 'not saved',
      });
      expect(registry.get(folders.state)).toEqual({local: '/data'});
    }),
  );
});
