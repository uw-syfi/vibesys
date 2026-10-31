import {describe, expect, test} from 'bun:test';
import type {InstanceList} from '@vibesys/contracts/host-helper';
import {
  type HostListing,
  type HostRun,
  hostListing,
  hostsComeUp,
  instanceObservation,
  instancePhaseText,
} from './listing.js';

describe('instancePhaseText', () => {
  test('a starting run reads as its stage and progress, so a long image build is visible', () => {
    expect(
      instancePhaseText({
        status: 'starting',
        stage: 'building_image',
        progress: {done: 12, total: 30, label: 'RUN pip install'},
      }),
    ).toBe('Building image 12/30: RUN pip install');
    expect(
      instancePhaseText({
        status: 'starting',
        stage: 'building_image',
        progress: {done: 3, total: null},
      }),
    ).toBe('Building image 3/?');
    expect(instancePhaseText({status: 'starting', stage: 'resolving'})).toBe('Resolving');
  });

  test('every startup stage has its own words', () => {
    const stages = [
      'resolving',
      'probing_workspace',
      'building_image',
      'building_tools',
      'starting_container',
    ] as const;
    const words = stages.map(stage => instancePhaseText({status: 'starting', stage}));
    expect(new Set(words).size).toBe(stages.length);
    expect(words).not.toContain(undefined);
  });

  test('any other phase reads as its status', () => {
    for (const status of ['running', 'stopping', 'stopped', 'failed', 'completed'] as const) {
      expect(instancePhaseText({status})).toBe(status);
    }
  });
});

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

const LETTERS = 'abcdefghijklmnopqrstuvwxyz';

/** 0 to 4 short arbitrary ids, built from `random`. */
function arbitraryIds(random: () => number): string[] {
  const count = Math.floor(random() * 5);
  return Array.from({length: count}, () => {
    const length = 1 + Math.floor(random() * 3);
    let id = '';
    for (let letter = 0; letter < length; letter += 1) {
      id += LETTERS[Math.floor(random() * LETTERS.length)];
    }
    return id;
  });
}

describe('hostListing', () => {
  const EMPTY: InstanceList = {instances: []};

  test('an empty listing reads as no live, ended, unreadable, or unverified runs', () => {
    expect(hostListing('local', EMPTY)).toEqual({
      kind: 'listed',
      host: 'local',
      live: [],
      ended: [],
      unreadable: [],
      unverified: [],
    });
  });

  test('regression: an unreadable record and an unverified id stay in their own fields', () => {
    // `unreadable` (a record of another VibeSys version) and `unverified` (an id whose lock could
    // not be read) are distinct verdicts (`server/registry.py`'s `survey()`): every id gets exactly
    // one. Merging them into one field previously mislabeled an unverified id as belonging to
    // another VibeSys version.
    const result = hostListing('local', {
      ...EMPTY,
      unreadable: [{id: 'incompat0001', version: 5}],
      unverified: ['unverified02'],
    });
    if (result.kind !== 'listed') throw new Error('expected a listed result');
    expect(result.unreadable).toEqual(['incompat0001']);
    expect(result.unverified).toEqual(['unverified02']);
  });

  test('each field reads sorted, independently of the other and of input order', () => {
    const result = hostListing('local', {
      ...EMPTY,
      unreadable: [
        {id: 'c', version: 1},
        {id: 'a', version: 1},
        {id: 'b', version: 1},
      ],
      unverified: ['z', 'x', 'y'],
    });
    if (result.kind !== 'listed') throw new Error('expected a listed result');
    expect(result.unreadable).toEqual(['a', 'b', 'c']);
    expect(result.unverified).toEqual(['x', 'y', 'z']);
  });

  test('property: unreadable and unverified ids never cross into the other field, for any mix', () => {
    const random = generator(20_261_011);
    for (let sample = 0; sample < 300; sample += 1) {
      const unreadableIds = arbitraryIds(random);
      const unverifiedIds = arbitraryIds(random);
      const result = hostListing('local', {
        ...EMPTY,
        unreadable: unreadableIds.map(id => ({id, version: 5})),
        unverified: unverifiedIds,
      });
      if (result.kind !== 'listed') throw new Error('expected a listed result');
      expect(result.unreadable).toEqual([...unreadableIds].sort());
      expect(result.unverified).toEqual([...unverifiedIds].sort());
    }
  });
});

describe('instanceObservation', () => {
  const TARGET = 'target';
  const KINDS = ['live', 'ended', 'unverified', 'unreadable'] as const;
  const run = (id: string): HostRun => ({
    id,
    projectRoot: '/p',
    runId: `run-${id}`,
    status: 'running',
    phase: 'running',
    paused: null,
    end: null,
    startedAt: 1,
    reason: null,
  });
  const when = <T>(mask: number, bit: number, value: T): T[] => ((mask & bit) === 0 ? [] : [value]);

  test('valid records win overlaps, then unverified, unreadable, and absence in that order', () => {
    for (let mask = 0; mask < 16; mask += 1) {
      const listing = {
        kind: 'listed' as const,
        host: 'local' as const,
        live: when(mask, 1, run(TARGET)),
        ended: when(mask, 2, run(TARGET)),
        unverified: when(mask, 4, TARGET),
        unreadable: when(mask, 8, TARGET),
      };

      const expected = KINDS.find((_kind, index) => (mask & (1 << index)) !== 0) ?? 'absent';
      expect(instanceObservation(listing, TARGET).kind).toBe(expected);
    }
  });

  test('unrelated ids never change the target observation', () => {
    const listing = {
      kind: 'listed' as const,
      host: 'local' as const,
      live: [run('live')],
      ended: [run('ended')],
      unverified: ['unverified'],
      unreadable: ['unreadable'],
    };
    expect(instanceObservation(listing, TARGET)).toEqual({kind: 'absent'});
  });
});

describe('hostsComeUp', () => {
  const listed = (host: string): HostListing => ({
    kind: 'listed',
    host,
    live: [],
    ended: [],
    unreadable: [],
    unverified: [],
  });
  const down = (host: string): HostListing => ({
    kind: 'unavailable',
    host,
    step: 'gateway',
    connection: {kind: 'offline', detail: 'unreachable'},
  });

  test('a host that answers now and did not before has come up; one that always did has not', () => {
    expect(hostsComeUp([down('a'), listed('b')], [listed('a'), listed('b')])).toEqual(['a']);
    expect(hostsComeUp([], [listed('a')])).toEqual(['a']);
    expect(hostsComeUp([listed('a')], [listed('a')])).toEqual([]);
    expect(hostsComeUp([listed('a')], [down('a')])).toEqual([]);
  });
});
