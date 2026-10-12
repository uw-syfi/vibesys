import {describe, expect, test} from 'bun:test';
import type {InstanceList} from '@vibesys/contracts/host-helper';
import {hostListing, instancePhaseText} from './listing.js';

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
