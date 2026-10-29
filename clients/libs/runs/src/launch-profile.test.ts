import {describe, expect, test} from 'bun:test';
import {
  applyLaunchProfileMutation,
  canonicalLaunchArguments,
  LAUNCH_ARGUMENT_BYTE_LIMIT,
  LAUNCH_ARGUMENT_COUNT_LIMIT,
  LAUNCH_ARGUMENT_TOTAL_BYTE_LIMIT,
  LAUNCH_PROFILE_LABEL_BYTE_LIMIT,
  LAUNCH_PROFILE_LABEL_CODE_POINT_LIMIT,
  LAUNCH_PROFILE_LIMIT,
  LaunchProfileConflictError,
  LaunchProfileValidationError,
  launchArguments,
  launchProfileId,
  parseLaunchProfile,
  parseLaunchProfileDraft,
  parseLaunchProfiles,
  shellWords,
} from '@vibesys/runs';
import {Seeded} from '@vibesys/store/testing';

const ID_1 = launchProfileId('00000000-0000-4000-8000-000000000001');
const ID_2 = launchProfileId('00000000-0000-4000-8000-000000000002');

function draft(label = 'Queue baseline') {
  return parseLaunchProfileDraft({
    label,
    host: 'ssh:gpu-box',
    project: '/srv/queue-rs',
    task: 'mpmc',
    arguments: ['--rounds', '4', '--goal', 'fast and correct'],
  });
}

function profile(overrides: Readonly<Record<string, unknown>> = {}) {
  return parseLaunchProfile({
    id: ID_1,
    revision: 1,
    ...draft(),
    ...overrides,
  });
}

describe('launch profile contract', () => {
  test('parses one bounded profile and keeps its typed vector immutable', () => {
    const parsed = profile();
    expect({...parsed, arguments: [...parsed.arguments]}).toEqual({
      id: ID_1,
      revision: 1,
      label: 'Queue baseline',
      host: 'ssh:gpu-box',
      project: '/srv/queue-rs',
      task: 'mpmc',
      arguments: ['--rounds', '4', '--goal', 'fast and correct'],
    });
    expect(Object.isFrozen(parsed)).toBe(true);
    expect(Object.isFrozen(parsed.arguments)).toBe(true);
  });

  test('rejects unknown, inherited, and malformed fields at their exact path', () => {
    expect(() => profile({surprise: true})).toThrow('launch profile.surprise is not allowed');

    const inherited = Object.create({surprise: true}) as Record<string, unknown>;
    Object.assign(inherited, {id: ID_1, revision: 1, ...draft()});
    expect(() => parseLaunchProfile(inherited, 'profiles[3]')).toThrow(
      'profiles[3].surprise must not be inherited',
    );
    const hiddenInherited = Object.create(
      Object.defineProperty({}, 'surprise', {value: true}),
    ) as Record<string, unknown>;
    Object.assign(hiddenInherited, {id: ID_1, revision: 1, ...draft()});
    expect(() => parseLaunchProfile(hiddenInherited, 'profiles[4]')).toThrow(
      'profiles[4].surprise must not be inherited',
    );
    const inheritedId = Object.create(Object.defineProperty({}, 'id', {value: ID_1})) as Record<
      string,
      unknown
    >;
    Object.assign(inheritedId, {revision: 1, ...draft()});
    expect(() => parseLaunchProfile(inheritedId, 'profiles[5]')).toThrow(
      'profiles[5].id must not be inherited',
    );
    const hiddenOwn = Object.defineProperty({...profile()}, 'surprise', {value: true});
    expect(() => parseLaunchProfile(hiddenOwn, 'profiles[6]')).toThrow(
      'profiles[6].surprise is not allowed',
    );
    const symbolOwn = {...profile(), [Symbol('surprise')]: true};
    expect(() => parseLaunchProfile(symbolOwn, 'profiles[7]')).toThrow(
      'profiles[7] must not contain symbol keys',
    );

    for (const [overrides, message] of [
      [{id: 'not-a-uuid'}, 'launch profile.id must be a lowercase canonical UUID'],
      [{revision: 0}, 'launch profile.revision must be a positive safe integer'],
      [{label: ' padded '}, 'launch profile.label must be one line'],
      [{host: 'ssh:-option'}, 'launch profile.host'],
      [{project: 'relative'}, 'launch profile.project must be an absolute path'],
      [{task: 'Bad Task'}, 'launch profile.task must be 1-128 lowercase'],
      [{arguments: ['ok', 1]}, 'launch profile.arguments[1] must be a string'],
      [{arguments: undefined}, 'launch profile.arguments must be a list of strings'],
    ] as const) {
      expect(() => profile(overrides)).toThrow(message);
      expect(() => profile(overrides)).toThrow(LaunchProfileValidationError);
    }
  });

  test('enforces the named label, token, and UTF-8 byte bounds', () => {
    expect(profile({label: 'a'.repeat(LAUNCH_PROFILE_LABEL_CODE_POINT_LIMIT)}).label).toHaveLength(
      LAUNCH_PROFILE_LABEL_CODE_POINT_LIMIT,
    );
    expect(() => profile({label: 'a'.repeat(LAUNCH_PROFILE_LABEL_CODE_POINT_LIMIT + 1)})).toThrow(
      `${LAUNCH_PROFILE_LABEL_CODE_POINT_LIMIT} characters`,
    );

    const emojiAtByteLimit = '😀'.repeat(LAUNCH_PROFILE_LABEL_BYTE_LIMIT / 4);
    expect(profile({label: emojiAtByteLimit}).label).toBe(emojiAtByteLimit);
    expect(() => profile({label: `${emojiAtByteLimit}😀`})).toThrow(
      `${LAUNCH_PROFILE_LABEL_BYTE_LIMIT} UTF-8 bytes`,
    );

    expect(
      launchArguments(Array.from({length: LAUNCH_ARGUMENT_COUNT_LIMIT}, () => 'x')),
    ).toHaveLength(LAUNCH_ARGUMENT_COUNT_LIMIT);
    expect(() =>
      launchArguments(Array.from({length: LAUNCH_ARGUMENT_COUNT_LIMIT + 1}, () => 'x')),
    ).toThrow(`${LAUNCH_ARGUMENT_COUNT_LIMIT} tokens`);
    expect(() => launchArguments(['é'.repeat(LAUNCH_ARGUMENT_BYTE_LIMIT / 2 + 1)])).toThrow(
      `${LAUNCH_ARGUMENT_BYTE_LIMIT} UTF-8 bytes`,
    );
    const fullToken = 'x'.repeat(LAUNCH_ARGUMENT_BYTE_LIMIT);
    expect(() =>
      launchArguments(
        Array.from(
          {length: LAUNCH_ARGUMENT_TOTAL_BYTE_LIMIT / fullToken.length + 1},
          () => fullToken,
        ),
      ),
    ).toThrow(`${LAUNCH_ARGUMENT_TOTAL_BYTE_LIMIT} UTF-8 bytes`);
    for (const token of ['line\nbreak', 'nul\0byte', '\ud800']) {
      expect(() => launchArguments([token])).toThrow(LaunchProfileValidationError);
    }
  });

  test('rejects duplicate ids and lists beyond the durable bound', () => {
    expect(() => parseLaunchProfiles([profile(), profile()])).toThrow(
      `launch profiles[1].id duplicates ${ID_1}`,
    );
    expect(() =>
      parseLaunchProfiles(
        Array.from({length: LAUNCH_PROFILE_LIMIT + 1}, (_, index) => ({
          ...profile(),
          id: `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
        })),
      ),
    ).toThrow(`${LAUNCH_PROFILE_LIMIT} profiles`);
  });

  test('rejects sparse, inherited, named, and symbol list entries', () => {
    const sparse = new Array<string>(1);
    expect(() => launchArguments(sparse, 'arguments')).toThrow('arguments[0] must be provided');
    expect(() => parseLaunchProfiles(new Array(1), 'profiles')).toThrow(
      'profiles[0] must be provided',
    );

    const inherited = new Array<string>(1);
    const inheritedPrototype = Object.assign(Object.create(Array.prototype), {0: 'inherited'});
    Object.setPrototypeOf(inherited, inheritedPrototype);
    expect(() => launchArguments(inherited, 'arguments')).toThrow(
      'arguments must have the standard array prototype',
    );

    const hiddenNamed = Object.defineProperty(['ok'], 'surprise', {value: true});
    expect(() => launchArguments(hiddenNamed, 'arguments')).toThrow(
      'arguments.surprise is not allowed',
    );
    const symbolNamed = Object.assign(['ok'], {[Symbol('surprise')]: true});
    expect(() => launchArguments(symbolNamed, 'arguments')).toThrow(
      'arguments must not contain symbol keys',
    );
  });

  test('canonical arguments are the one-line, exact source for a future form', () => {
    const random = new Seeded(2718);
    const alphabet = [...`ab -'"$\`\\\t;*é😀`];
    for (let round = 0; round < 500; round += 1) {
      const tokens = Array.from({length: random.int(8)}, () =>
        Array.from({length: random.int(20)}, () => random.pick(alphabet)).join(''),
      );
      const validated = launchArguments(tokens);
      const summary = canonicalLaunchArguments(validated);
      expect(shellWords(summary)).toEqual(tokens);
      expect(canonicalLaunchArguments(shellWords(summary))).toBe(summary);
    }
  });
});

describe('optimistic launch profile mutations', () => {
  test('save, rename, and delete return a new authoritative list', () => {
    const saved = applyLaunchProfileMutation([], {kind: 'save', id: ID_1, profile: draft()});
    expect(saved).toEqual([profile()]);

    const renamed = applyLaunchProfileMutation(saved, {
      kind: 'rename',
      id: ID_1,
      revision: 1,
      label: 'Queue nightly',
    });
    expect(renamed[0]).toEqual({...profile(), revision: 2, label: 'Queue nightly'});
    expect(saved[0]?.label).toBe('Queue baseline');

    const deleted = applyLaunchProfileMutation(renamed, {
      kind: 'delete',
      id: ID_1,
      revision: 2,
    });
    expect(deleted).toEqual([]);
  });

  test('stale, missing, duplicate, and overflowing mutations fail without changing their input', () => {
    const profiles = applyLaunchProfileMutation([], {kind: 'save', id: ID_1, profile: draft()});
    const before = JSON.stringify(profiles);
    for (const mutation of [
      {kind: 'save', id: ID_1, profile: draft('Duplicate')},
      {kind: 'rename', id: ID_1, revision: 2, label: 'Stale'},
      {kind: 'delete', id: ID_2, revision: 1},
    ] as const) {
      expect(() => applyLaunchProfileMutation(profiles, mutation)).toThrow(
        LaunchProfileConflictError,
      );
      expect(JSON.stringify(profiles)).toBe(before);
    }
    expect(() =>
      applyLaunchProfileMutation([profile({revision: Number.MAX_SAFE_INTEGER})], {
        kind: 'rename',
        id: ID_1,
        revision: Number.MAX_SAFE_INTEGER,
        label: 'No overflow',
      }),
    ).toThrow('cannot be revised again');
  });

  test('save rejects a structurally typed draft that carries identity fields', () => {
    const withIdentity = {...draft(), id: ID_2, revision: 99};
    const structurallyTypedDraft: ReturnType<typeof draft> = withIdentity;
    expect(() =>
      applyLaunchProfileMutation([], {
        kind: 'save',
        id: ID_1,
        profile: structurallyTypedDraft,
      }),
    ).toThrow('launch profile.id is not allowed');
  });

  test('model: only the exact current revision can mutate a profile', () => {
    const random = new Seeded(118);
    let profiles = applyLaunchProfileMutation([], {kind: 'save', id: ID_1, profile: draft()});
    for (let round = 0; round < 300; round += 1) {
      const current = profiles[0];
      if (current === undefined) throw new Error('profile disappeared');
      const stale = current.revision + 1 + random.int(20);
      const before = JSON.stringify(profiles);
      const staleMutation = random.bool()
        ? ({kind: 'rename', id: ID_1, revision: stale, label: `Stale ${round}`} as const)
        : ({kind: 'delete', id: ID_1, revision: stale} as const);
      expect(() => applyLaunchProfileMutation(profiles, staleMutation)).toThrow(
        LaunchProfileConflictError,
      );
      expect(JSON.stringify(profiles)).toBe(before);

      profiles = applyLaunchProfileMutation(profiles, {
        kind: 'rename',
        id: ID_1,
        revision: current.revision,
        label: `Profile ${round}`,
      });
      expect(profiles[0]?.revision).toBe(current.revision + 1);
    }
  });
});
