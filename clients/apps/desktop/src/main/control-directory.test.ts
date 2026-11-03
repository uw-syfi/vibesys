import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Schema from 'effect/Schema';
import {
  type ControlFilesystem,
  controlDirectory,
  type DirectoryEntry,
} from './control-directory.js';

/** One path, existing as `entry` (or absent), created private by this user when absent. */
const filesystem = (entry: DirectoryEntry | null, uid: number): ControlFilesystem => {
  let current = entry;
  return {
    createPrivate: () =>
      Effect.sync(() => {
        if (current !== null) return false;
        current = {kind: 'directory', uid, mode: 0o40700};
        return true;
      }),
    inspect: () => Effect.sync(() => current ?? {kind: 'other', uid: -1, mode: 0}),
  };
};

describe('control directory', () => {
  it.effect('creates a private directory and returns its short path', () =>
    Effect.gen(function* () {
      expect(yield* controlDirectory(1000, filesystem(null, 1000))).toBe('/tmp/vsd-1000');
    }),
  );

  it.effect.prop(
    'trusts an existing directory only when it is a private directory of this user',
    {
      kind: Schema.Literals(['directory', 'other']),
      uid: Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 2000})),
      permissions: Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 0o777})),
    },
    ({kind, uid, permissions}) =>
      Effect.gen(function* () {
        const outcome = yield* Effect.result(
          controlDirectory(1000, filesystem({kind, uid, mode: permissions}, 1000)),
        );
        const trusted = kind === 'directory' && uid === 1000 && (permissions & 0o077) === 0;
        expect(outcome._tag === 'Success').toBe(trusted);
        if (outcome._tag === 'Failure') expect(outcome.failure.message).toContain('/tmp/vsd-1000');
      }),
  );

  it.effect("fails with the filesystem's reason when the directory cannot be created", () =>
    Effect.gen(function* () {
      const broken: ControlFilesystem = {
        createPrivate: () => Effect.fail({_tag: 'GatewayFailed', message: 'no space'} as never),
        inspect: () => Effect.die('unreachable'),
      };
      const failure = yield* Effect.flip(controlDirectory(1, broken));
      expect(failure.message).toBe('no space');
    }),
  );
});
