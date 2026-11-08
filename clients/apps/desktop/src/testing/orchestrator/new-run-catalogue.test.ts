import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  FitState,
  oneofIs,
  ProvisionRefusal,
  ProvisionRepositoryRequestSchema,
} from '@vibesys/orchestrator-proto';
import {entryKey, fits, readable} from '../../state/index.js';
import {richCatalogueResponse, richPlaces, richProvisionAnswer} from './new-run-catalogue.js';
import {fitOf} from './new-run-fixtures.js';

const ask = (example: string, name: string, parent = '') =>
  create(ProvisionRepositoryRequestSchema, {
    requestId: 'r',
    name,
    source: {case: 'example', value: example},
    parentDirectory: parent,
  });

describe('the fixture host’s whole catalogue', () => {
  const {entries} = richCatalogueResponse();

  it('has CPU and GPU entries in three domains, with the host’s fit on each over its places', () => {
    const domains = new Set(entries.map(entry => entry.card?.domain));
    expect(domains.size).toBeGreaterThanOrEqual(3);
    expect(entries.some(fits)).toBe(true);
    expect(entries.some(entry => entry.fit?.state === FitState.NO_ACCELERATOR)).toBe(true);
    for (const entry of entries) {
      if (entry.card !== undefined) expect(entry.fit).toEqual(fitOf(entry.card, richPlaces()));
    }
  });

  it('tells apart two tasks of one example, and never names two entries the same', () => {
    const keys = entries.map(entryKey);
    expect(new Set(keys).size).toBe(keys.length);
    expect(entries.filter(entry => entry.example === 'data-structures/prioqueue')).toHaveLength(2);
  });

  it('has a place the host cannot read, which fits nothing', () => {
    const unreadable = richPlaces().filter(place => !readable(place));
    expect(unreadable.map(place => place.name)).toEqual(['cluster-b']);
    for (const entry of entries) expect(entry.fit?.places).not.toContain('cluster-b');
  });
});

describe('the fixture host’s provisioning', () => {
  it('copies a real example, expanding ~, and refuses it the second time under that name', () => {
    const taken = new Set<string>();
    const first = richProvisionAnswer(ask('data-structures/prioqueue', 'pq', '~/work'), taken);
    expect(first.outcome).toMatchObject({
      case: 'provisioned',
      value: {repository: '/home/user/work/pq'},
    });
    taken.add('/home/user/work/pq');
    const again = richProvisionAnswer(ask('data-structures/prioqueue', 'pq', '~/work'), taken);
    expect(again.outcome).toMatchObject({
      case: 'refused',
      value: {reason: ProvisionRefusal.NAME_TAKEN},
    });
  });

  it('refuses a read-only folder, an example that clones, and one it has none of', () => {
    const taken = new Set<string>();
    const reason = (example: string, parent = '') => {
      const {outcome} = richProvisionAnswer(ask(example, 'x', parent), taken);
      return oneofIs(outcome, 'refused', {provisioned: true, refused: true})
        ? outcome.value.reason
        : undefined;
    };
    expect(reason('data-structures/prioqueue', '/usr/local')).toBe(
      ProvisionRefusal.PARENT_NOT_WRITABLE,
    );
    expect(reason('database/differential-dataflow')).toBe(ProvisionRefusal.NEEDS_NETWORK);
    expect(reason('nothing/here')).toBe(ProvisionRefusal.EXAMPLE_NOT_FOUND);
  });
});
